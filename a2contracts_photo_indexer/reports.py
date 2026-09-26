"""Daily reports: writing the note at the top of a project's draft report
(the owner, 2026-09-26: "a quick prefill that will take info from
everything... make use of our local ai agent").

The server owns the prompt and turns the draft into plain text -- photos
with what this indexer already saw in them, permit and inspection changes,
new plan sheets, 3D models -- and adds a few of the day's photos. This side
shows both to the Qwen model and posts the note back; the app fills the
draft with it unless a person has already written their own.
API (company admin): GET /api/ai/reports/pending/?worker=,
POST /api/ai/reports/<id>/suggestion/ {basis, text, model}.
"""
from __future__ import annotations

import re
import sys
import time

import requests

from .indexer import WORKER, load_image


def pending(api) -> dict:
    r = api.request('GET', f'/api/ai/reports/pending/?worker={requests.utils.quote(WORKER)}')
    r.raise_for_status()
    return r.json()


def _clean(text: str) -> str:
    """Plain sentences: no markdown, no "Note:" preamble, no quotes around it."""
    text = re.sub(r'[*_#`]+', '', text.strip())
    text = re.sub(r'^\s*(here is|here\'s)[^:\n]*:\s*', '', text, flags=re.I)
    text = re.sub(r'^\s*(note|summary)\s*:\s*', '', text, flags=re.I)
    text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.M)
    text = ' '.join(line.strip() for line in text.splitlines() if line.strip())
    return text.strip().strip('"').strip()


def process_reports(api, captioner) -> int:
    """One round: writes the note for every draft waiting for one. Returns
    how many were taken (0 = nothing pending). Needs the Qwen captioner --
    the only model here that takes a free prompt."""
    if captioner is None or not hasattr(captioner, 'ask'):
        return 0
    batch = pending(api)
    reports = batch.get('reports') or []
    if not reports:
        return 0
    prompt = batch['prompt']
    print(f'{len(reports)} daily report note(s) to write')
    for row in reports:
        started = time.time()
        images = []
        for url in row.get('image_urls') or []:
            try:
                image = load_image(api.image(url))
                image.thumbnail((640, 640))  # a glance at the day, not a caption
                images.append(image)
            except Exception as exc:  # noqa: BLE001 -- the text alone still makes a note
                print(f"report #{row['id']}: skipped a photo ({exc})", file=sys.stderr)
        try:
            text = _clean(captioner.ask(images, f"{prompt}\n\n{row['context']}", max_new_tokens=220))
        except Exception as exc:  # noqa: BLE001 -- one bad draft must not stop the run
            print(f"report #{row['id']} FAILED: {exc}", file=sys.stderr)
            continue  # the lease runs out and the server hands it out again
        if not text:
            continue
        try:
            r = api.request('POST', f"/api/ai/reports/{row['id']}/suggestion/", json={'basis': row['basis'], 'text': text, 'model': captioner.name})
            if r.status_code != 200:
                raise RuntimeError(f'rejected ({r.status_code}): {r.text[:300]}')
            print(f"report #{row['id']} {row['project_name'][:24]:24s} {time.time() - started:5.1f}s  {text[:80]}")
        except Exception as exc:  # noqa: BLE001
            print(f"report #{row['id']} could not post the note: {exc}", file=sys.stderr)
            time.sleep(5)
    return len(reports)
