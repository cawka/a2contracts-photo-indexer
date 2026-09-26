"""Plan sheets: reading the title block of pages the server's text-layer
detection couldn't name (scans, outlined title blocks) -- the local
stand-in for the Gemini fallback (the owner, 2026-09-21: "can we just
use local AI for that (like the worker we use for pic annotations)?").

The server owns the prompt and the two views (whole page + bottom-right
quarter); this side only shows them to the Qwen VL model and posts the
JSON back. Sheets a person named by hand never reach this queue.
API (company admin): GET /api/ai/plans/pending/?limit=&worker=,
GET /api/ai/plans/<id>/views/, POST /api/ai/plans/<id>/detection/,
GET /api/ai/plans/stats/.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time

import requests

from .indexer import WORKER, load_image


def _parse(text: str) -> tuple[str, str]:
    match = re.search(r'\{.*\}', text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return str(data.get('sheet_number') or '').strip(), str(data.get('sheet_title') or '').strip()
        except ValueError:
            pass
    number = re.search(r'"sheet_number"\s*:\s*"([^"]*)"', text)
    title = re.search(r'"sheet_title"\s*:\s*"([^"]*)"', text)
    return (number.group(1).strip() if number else ''), (title.group(1).strip() if title else '')


def pending(api, limit: int) -> dict:
    r = api.request('GET', f'/api/ai/plans/pending/?limit={limit}&worker={requests.utils.quote(WORKER)}')
    r.raise_for_status()
    return r.json()


def stats(api) -> dict:
    r = api.request('GET', '/api/ai/plans/stats/')
    r.raise_for_status()
    return r.json()


def process_plans(api, captioner, limit: int) -> int:
    """One round: reads up to `limit` pending sheets. Returns how many
    were taken (0 = nothing pending). Needs the Qwen captioner -- the
    only model here that takes a free prompt."""
    if captioner is None or not hasattr(captioner, 'ask'):
        return 0
    batch = pending(api, limit)
    sheets = batch.get('sheets') or []
    if not sheets:
        return 0
    prompt = batch['prompt']
    print(f'{len(sheets)} plan sheet(s) to read')
    for row in sheets:
        started = time.time()
        try:
            views = api.request('GET', row['views_url'][len(api.base):] if row['views_url'].startswith(api.base) else row['views_url'])
            views.raise_for_status()
            views = views.json()
            images = [load_image(base64.b64decode(views['full'])), load_image(base64.b64decode(views['corner']))]
            number, title = _parse(captioner.ask(images, prompt, max_new_tokens=120))
        except Exception as exc:  # noqa: BLE001 -- one bad page must not stop the run
            print(f"sheet #{row['id']} FAILED: {exc}", file=sys.stderr)
            number, title = '', ''
        try:
            r = api.request('POST', f"/api/ai/plans/{row['id']}/detection/", json={'sheet_number': number, 'sheet_title': title, 'model': captioner.name})
            if r.status_code != 200:
                raise RuntimeError(f'rejected ({r.status_code}): {r.text[:300]}')
            result = r.json()
            print(f"sheet #{row['id']} {row['project_name'][:24]:24s} p{row['page']:<3} {time.time() - started:5.1f}s  {number or '(nothing)'} {title[:50]}" + (f"  -> {result['sheet_number']} {result['title']}" if result.get('changed') else ''))
        except Exception as exc:  # noqa: BLE001
            print(f"sheet #{row['id']} could not post the reading: {exc}", file=sys.stderr)
            time.sleep(5)
    return len(sheets)


# --- OCR for scanned pages ---------------------------------------------------
#
# A scanned sheet has no text layer, so the app's "search inside the
# pages" has nothing to read until a worker OCRs it. RapidOCR (ONNX, CPU,
# pip-installable, no system packages) reads the page image the server
# renders and posts every word with its box; the server scales the boxes
# into fractions of the page itself. `--no-ocr` skips this queue, and it
# is silently skipped when rapidocr isn't installed.

_OCR = {'engine': None, 'name': '', 'tried': False}


def _ocr_engine():
    if _OCR['tried']:
        return _OCR['engine']
    _OCR['tried'] = True
    # rapidocr-onnxruntime stops at Python 3.12; its successor `rapidocr`
    # (3.x, on onnxruntime) covers newer Pythons.
    try:
        from rapidocr import RapidOCR

        _OCR['engine'] = RapidOCR()
        _OCR['name'] = 'rapidocr'
    except Exception:  # noqa: BLE001 -- optional dependency
        try:
            from rapidocr_onnxruntime import RapidOCR

            _OCR['engine'] = RapidOCR()
            _OCR['name'] = 'rapidocr-onnxruntime'
        except Exception as exc:  # noqa: BLE001
            print(f'plan OCR off (pip install ".[ocr]" to turn it on): {exc}', file=sys.stderr)
            _OCR['engine'] = None
    return _OCR['engine']


def ocr_pending(api, limit: int) -> dict:
    r = api.request('GET', f'/api/ai/plans/ocr/pending/?limit={limit}&worker={requests.utils.quote(WORKER)}')
    r.raise_for_status()
    return r.json()


def ocr_stats(api) -> dict:
    r = api.request('GET', '/api/ai/plans/ocr/stats/')
    r.raise_for_status()
    return r.json()


def process_plan_ocr(api, limit: int) -> int:
    """One round: OCRs up to `limit` scanned pages. Returns how many were
    taken (0 = nothing pending, or no OCR engine here)."""
    engine = _ocr_engine()
    if engine is None:
        return 0
    batch = ocr_pending(api, limit)
    sheets = batch.get('sheets') or []
    if not sheets:
        return 0
    print(f'{len(sheets)} scanned page(s) to OCR')
    for row in sheets:
        started = time.time()
        try:
            image = api.request('GET', row['image_url'][len(api.base):] if row['image_url'].startswith(api.base) else row['image_url'])
            image.raise_for_status()
            words, width, height = _read(engine, image.content)
        except Exception as exc:  # noqa: BLE001 -- one bad page must not stop the run
            print(f"sheet #{row['id']} OCR FAILED: {exc}", file=sys.stderr)
            words, width, height = [], 1, 1
        try:
            r = api.request('POST', f"/api/ai/plans/{row['id']}/ocr/", json={'words': words, 'image_width': width, 'image_height': height, 'engine': _OCR['name']}, timeout=180)
            if r.status_code != 200:
                raise RuntimeError(f'rejected ({r.status_code}): {r.text[:300]}')
            print(f"sheet #{row['id']} {row['project_name'][:24]:24s} {time.time() - started:6.1f}s  {len(words)} words")
        except Exception as exc:  # noqa: BLE001
            print(f"sheet #{row['id']} could not post the OCR: {exc}", file=sys.stderr)
            time.sleep(5)
    return len(sheets)


def _read(engine, image_bytes: bytes):
    """RapidOCR over the page image -> ([{text, box}], width, height).
    Its boxes are 4 corner points; the app wants an axis-aligned box."""
    import io

    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    array = np.asarray(image)
    result = engine(array)
    if hasattr(result, 'txts'):  # rapidocr 3.x: an output object
        entries = zip(result.boxes if result.boxes is not None else [], result.txts or ())
    else:  # rapidocr-onnxruntime: (list of [box, text, score], timings)
        entries = ((e[0], e[1]) for e in result[0] or [])
    words = []
    for box, text in entries:
        text = str(text or '').strip()
        if not text:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        # One detection can be a whole line; split it so a single word
        # highlights on its own, spreading the line's box across its words.
        parts = text.split()
        if len(parts) > 1:
            total = sum(len(p) for p in parts)
            x0, x1 = min(xs), max(xs)
            at = x0
            for part in parts:
                share = (x1 - x0) * (len(part) / total)
                words.append({'text': part, 'box': [at, min(ys), at + share, max(ys)]})
                at += share
        else:
            words.append({'text': text, 'box': [min(xs), min(ys), max(xs), max(ys)]})
    return words, image.width, image.height
