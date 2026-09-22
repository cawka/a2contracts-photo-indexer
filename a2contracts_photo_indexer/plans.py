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
