#!/usr/bin/env python3
"""The photo indexer: runs the AI models the server deliberately never
runs (docs/photos-design.md, phase 3) -- on the owner's GPU box or
laptop, whenever it happens to be on. It asks the server for photos
with no index yet, fetches each picture, computes

  - a caption + tags with a vision-language model (Qwen3-VL by default,
    Florence-2 as the light fallback),
  - an image embedding with SigLIP (for "similar photos"), and the
    SigLIP zero-shot match against a construction vocabulary (tags that
    stay consistent across photos, unlike free-text ones),

and posts them back. Nothing leaves the owner's machines but the results.

Usage (see README.md):

    a2-photo-indexer login     --api https://contracts.a2cons.com
    a2-photo-indexer run       --api https://contracts.a2cons.com [--once]   # AI index
    a2-photo-indexer transcode --api https://contracts.a2cons.com [--once]   # video renditions (transcode.py)
    a2-photo-indexer status    --api https://contracts.a2cons.com

The device-token pair from `login` is kept in ~/.a2contracts/photo-indexer.json
(mode 600) and refreshed automatically; `run` loops until stopped
(`--once` = one pass). `--captioner`/`--embedder` pick the models; the
combination is the index's `model` name, so changing models re-indexes
every photo for the new name and leaves the old rows in place until then.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import re
import signal
import socket

# What the app's queue shows as the holder of a lease (`status`).
WORKER = os.environ.get('A2_WORKER') or socket.gethostname()
import sys
import time
from pathlib import Path

import requests

TOKEN_FILE = Path(os.environ.get('A2_INDEXER_TOKEN_FILE', Path.home() / '.a2contracts' / 'photo-indexer.json'))

# Zero-shot vocabulary for SigLIP: what a jobsite photo tends to be of.
# Short noun phrases; "a photo of {x}" is the prompt.
VOCABULARY = [
    'framing', 'wood framing', 'steel framing', 'trusses', 'sheathing', 'drywall', 'insulation', 'stucco', 'siding',
    'roofing', 'shingles', 'metal roof', 'flashing', 'gutters', 'windows', 'doors', 'garage door', 'cabinets', 'countertops',
    'tile', 'flooring', 'hardwood floor', 'carpet', 'paint', 'trim and molding', 'stairs', 'railing', 'ceiling', 'lighting',
    'electrical panel', 'electrical wiring', 'conduit', 'outlets and switches', 'plumbing', 'pipes', 'water heater',
    'HVAC ducts', 'air handler', 'condenser unit', 'concrete slab', 'concrete pour', 'concrete forms', 'rebar', 'foundation',
    'footings', 'excavation', 'trench', 'grading', 'compaction', 'pavers', 'driveway', 'sidewalk', 'retaining wall',
    'masonry', 'block wall', 'brick', 'fence', 'gate', 'deck', 'pergola', 'pool', 'landscaping', 'irrigation', 'sod',
    'demolition', 'debris', 'dumpster', 'delivery', 'lumber', 'materials on a pallet', 'truck', 'excavator', 'crane',
    'scaffolding', 'ladder', 'tools', 'generator', 'porta potty', 'jobsite sign', 'permit or inspection tag', 'plans on paper',
    'measuring tape', 'level', 'survey stakes', 'water damage', 'mold', 'crack', 'leak', 'termite damage', 'rust',
    'kitchen', 'bathroom', 'bedroom', 'living room', 'garage', 'attic', 'crawl space', 'exterior of a house', 'roof view',
    'street view', 'backyard', 'front yard', 'workers', 'inspector', 'a document', 'a screenshot', 'a receipt',
]


# --- API client --------------------------------------------------------------------

class Api:
    def __init__(self, base: str):
        self.base = base.rstrip('/')
        self.http = requests.Session()
        self.creds = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(TOKEN_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(self.creds))
        os.chmod(TOKEN_FILE, 0o600)

    def login(self) -> None:
        email = input('Email: ').strip()
        password = getpass.getpass('Password: ')
        device = f'photo-indexer@{socket.gethostname()}'
        r = self.http.post(f'{self.base}/api/auth/token/', json={'email': email, 'password': password, 'device_name': device}, timeout=30)
        if r.status_code != 200:
            sys.exit(f'Login failed ({r.status_code}): {r.text[:200]}')
        data = r.json()
        if data.get('mfa_required'):
            code = input('MFA code: ').strip()
            r = self.http.post(f'{self.base}/api/auth/token/mfa/', json={'pending_token': data['pending_token'], 'code': code}, timeout=30)
            if r.status_code != 200:
                sys.exit(f'MFA failed ({r.status_code}): {r.text[:200]}')
            data = r.json()
        self.creds = {'access_token': data['access_token'], 'refresh_token': data['refresh_token'], 'device_id': data.get('device_id')}
        self._save()
        print(f'Signed in as {email}; token saved to {TOKEN_FILE}')

    def _headers(self) -> dict:
        if not self.creds.get('access_token'):
            sys.exit(f'Not signed in -- run: a2-photo-indexer login --api {self.base}')
        return {'Authorization': f"Bearer {self.creds['access_token']}", 'Accept': 'application/json'}

    def _refresh(self) -> bool:
        if not self.creds.get('refresh_token'):
            return False
        r = self.http.post(f'{self.base}/api/auth/token/refresh/', json={'refresh_token': self.creds['refresh_token']}, timeout=30)
        if r.status_code != 200:
            return False
        data = r.json()
        self.creds.update({'access_token': data['access_token'], 'refresh_token': data['refresh_token']})
        self._save()
        return True

    def request(self, method: str, path: str, *, retry_auth: bool = True, headers_extra: dict | None = None, **kw):
        headers = {**self._headers(), **(headers_extra or {})}
        r = self.http.request(method, f'{self.base}{path}', headers=headers, timeout=kw.pop('timeout', 120), **kw)
        if r.status_code == 401 and retry_auth and self._refresh():
            return self.request(method, path, retry_auth=False, headers_extra=headers_extra, **kw)
        if r.status_code == 401:
            sys.exit(f'Session expired -- run: a2-photo-indexer login --api {self.base}')
        return r

    def pending(self, model: str, limit: int) -> list[dict]:
        # The rows come back leased to this worker (`worker` names it in
        # the app's queue): a second machine polling gets other rows.
        r = self.request('GET', f'/api/ai/photos/pending/?model={requests.utils.quote(model)}&limit={limit}&worker={requests.utils.quote(WORKER)}')
        r.raise_for_status()
        return r.json()

    def stats(self, model: str) -> dict:
        r = self.request('GET', f'/api/ai/photos/stats/?model={requests.utils.quote(model)}')
        r.raise_for_status()
        return r.json()

    def image(self, url: str) -> bytes:
        r = self.request('GET', url[len(self.base):] if url.startswith(self.base) else url)
        r.raise_for_status()
        return r.content

    def release(self, purpose: str, ids: list[int]) -> None:
        """Hands back leased rows this worker will not get to (a stop
        mid-batch), so they are pending again at once."""
        if not ids:
            return
        try:
            self.request('POST', '/api/ai/claims/release/', json={'purpose': purpose, 'ids': ids}, timeout=30)
        except Exception as exc:  # noqa: BLE001 -- the lease expires on its own anyway
            print(f'could not release {len(ids)} rows: {exc}', file=sys.stderr)

    def post_index(self, photo_id: int, payload: dict) -> None:
        r = self.request('POST', f'/api/ai/photos/{photo_id}/index/', json=payload)
        if r.status_code != 200:
            raise RuntimeError(f'index rejected ({r.status_code}): {r.text[:300]}')


# --- Models -------------------------------------------------------------------------

def pick_device(requested: str) -> str:
    if requested != 'auto':
        return requested
    import torch

    if torch.cuda.is_available():
        return 'cuda'
    if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


class SiglipEmbedder:
    """Image embedding + zero-shot tags over VOCABULARY (open_clip)."""

    def __init__(self, model_name: str, device: str):
        import open_clip
        import torch

        self.device = device
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model_name, pretrained=None if ':' in model_name else 'webli')
        self.model = self.model.to(device).eval()
        tokenizer = open_clip.get_tokenizer(model_name)
        with torch.no_grad():
            text = tokenizer([f'a photo of {t}' for t in VOCABULARY]).to(device)
            feats = self.model.encode_text(text)
            self.vocab = (feats / feats.norm(dim=-1, keepdim=True)).float()
        self.logit_scale = float(self.model.logit_scale.exp()) if hasattr(self.model, 'logit_scale') else 100.0
        self.logit_bias = float(self.model.logit_bias) if getattr(self.model, 'logit_bias', None) is not None else 0.0
        self.name = model_name.split('/')[-1]

    def __call__(self, image) -> tuple[list[float], list[str]]:
        torch = self.torch
        with torch.no_grad():
            x = self.preprocess(image).unsqueeze(0).to(self.device)
            feat = self.model.encode_image(x)
            feat = (feat / feat.norm(dim=-1, keepdim=True)).float()
            # SigLIP's own sigmoid pairing: an independent probability per tag.
            logits = feat @ self.vocab.T * self.logit_scale + self.logit_bias
            probs = torch.sigmoid(logits)[0].tolist()
        tags = [VOCABULARY[i] for i in sorted(range(len(VOCABULARY)), key=lambda i: -probs[i])[:6] if probs[i] > 0.15]
        return feat[0].tolist(), tags


PROMPT = (
    'This is a construction jobsite photo taken by a contractor. Describe it in one or two factual sentences '
    '(what work is shown, what stage it is at, anything notable such as damage or a defect). Then list up to 8 short tags. '
    'Answer with JSON only: {"caption": "...", "tags": ["...", "..."]}'
)


def _parse_vlm(text: str) -> tuple[str, list[str]]:
    """The model's JSON, or what can be salvaged from one cut off by the
    token limit (a long caption plus eight tags can overrun it: seen
    2026-09-21, one photo stored its raw half-JSON as the caption)."""
    match = re.search(r'\{.*\}', text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            caption = str(data.get('caption') or '').strip()
            tags = [str(t).strip() for t in (data.get('tags') or []) if str(t).strip()]
            if caption:
                return caption, tags[:8]
        except ValueError:
            pass
    caption = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if caption:
        tags_part = text[caption.end():]
        tags = re.findall(r'"((?:[^"\\]|\\.)+)"', tags_part.split('[', 1)[1]) if '[' in tags_part else []
        return caption.group(1).strip()[:600], [t.strip() for t in tags if t.strip() and t.strip() != 'tags'][:8]
    return text.strip()[:600], []


class QwenCaptioner:
    """Caption + tags from a Qwen VL model (2.5-VL or 3-VL) via transformers."""

    def __init__(self, model_name: str, device: str):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        dtype = torch.float16 if device in ('cuda', 'mps') else torch.float32
        self.processor = AutoProcessor.from_pretrained(model_name)
        self.model = AutoModelForImageTextToText.from_pretrained(model_name, dtype=dtype, device_map=device if device == 'cuda' else None)
        if device != 'cuda':
            self.model = self.model.to(device)
        self.model.eval()
        self.device = device
        self.torch = torch
        self.name = model_name.split('/')[-1]

    def __call__(self, image) -> tuple[str, list[str]]:
        messages = [{'role': 'user', 'content': [{'type': 'image', 'image': image}, {'type': 'text', 'text': PROMPT}]}]
        inputs = self.processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors='pt').to(self.model.device)
        with self.torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=320, do_sample=False)
        text = self.processor.batch_decode(out[:, inputs['input_ids'].shape[1]:], skip_special_tokens=True)[0]
        return _parse_vlm(text)


class FlorenceCaptioner:
    """Florence-2's own detailed caption (no tags of its own)."""

    def __init__(self, model_name: str, device: str):
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        dtype = torch.float16 if device == 'cuda' else torch.float32
        self.processor = AutoProcessor.from_pretrained(model_name, trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype, trust_remote_code=True).to(device).eval()
        self.device, self.dtype, self.torch = device, dtype, torch
        self.name = model_name.split('/')[-1]

    def __call__(self, image) -> tuple[str, list[str]]:
        task = '<MORE_DETAILED_CAPTION>'
        inputs = self.processor(text=task, images=image, return_tensors='pt').to(self.device, self.dtype)
        with self.torch.no_grad():
            out = self.model.generate(input_ids=inputs['input_ids'], pixel_values=inputs['pixel_values'], max_new_tokens=120, num_beams=3)
        text = self.processor.batch_decode(out, skip_special_tokens=False)[0]
        parsed = self.processor.post_process_generation(text, task=task, image_size=(image.width, image.height))
        return str(parsed.get(task, '')).strip(), []


# --- The loop -------------------------------------------------------------------------

def load_image(data: bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((1024, 1024))
    return image


def run(args) -> None:
    api = Api(args.api)
    device = pick_device(args.device)
    print(f'device: {device}')
    captioner = None
    if args.captioner == 'qwen':
        captioner = QwenCaptioner(args.qwen_model, device)
    elif args.captioner == 'florence':
        captioner = FlorenceCaptioner(args.florence_model, device)
    embedder = SiglipEmbedder(args.siglip_model, device) if args.embedder == 'siglip' else None
    model_name = '+'.join(filter(None, [captioner.name if captioner else '', embedder.name if embedder else '']))[:120] or 'none'
    print(f'index model name: {model_name}')

    stop = {'now': False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__('now', True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__('now', True))

    while not stop['now']:
        stats = api.stats(model_name)
        pending = api.pending(model_name, args.batch)
        if not pending:
            busy = f', {stats["in_progress"]} with other workers' if stats.get('in_progress') else ''
            print(f'nothing pending ({stats["indexed"]}/{stats["total"]} indexed{busy})')
            if args.once:
                return
            for _ in range(args.interval):
                if stop['now']:
                    return
                time.sleep(1)
            continue
        print(f'{stats["pending"]} pending; taking {len(pending)}')
        for n, row in enumerate(pending):
            if stop['now']:
                api.release('index', [r['id'] for r in pending[n:]])
                return
            started = time.time()
            try:
                image = load_image(api.image(row['url']))
                caption, tags = captioner(image) if captioner else ('', [])
                payload = {'model': model_name, 'caption': caption, 'tags': tags}
                if embedder:
                    vector, vocab_tags = embedder(image)
                    import numpy as np

                    payload['embedding'] = base64.b64encode(np.asarray(vector, dtype='<f2').tobytes()).decode()
                    payload['dim'] = len(vector)
                    payload['tags'] = list(dict.fromkeys([*tags, *vocab_tags]))
                api.post_index(row['id'], payload)
                print(f"#{row['id']} {row['project_name'][:30]:30s} {time.time() - started:5.1f}s  {caption[:70]}")
            except Exception as exc:  # noqa: BLE001 -- one bad photo must not stop the run
                print(f"#{row['id']} FAILED: {exc}", file=sys.stderr)
                # Mark it so the queue moves on; the caption says why.
                try:
                    api.post_index(row['id'], {'model': model_name, 'caption': '', 'tags': ['indexing failed']})
                except Exception as exc2:  # noqa: BLE001
                    print(f"#{row['id']} could not even record the failure: {exc2}", file=sys.stderr)
                    time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description='A2 Contracts photo indexer')
    parser.add_argument('command', choices=['login', 'run', 'status', 'transcode'])
    parser.add_argument('--api', default=os.environ.get('A2_API', 'https://contracts.a2cons.com'))
    parser.add_argument('--captioner', choices=['qwen', 'florence', 'none'], default='qwen')
    parser.add_argument('--qwen-model', default='Qwen/Qwen3-VL-8B-Instruct')
    parser.add_argument('--florence-model', default='microsoft/Florence-2-large')
    parser.add_argument('--embedder', choices=['siglip', 'none'], default='siglip')
    parser.add_argument('--siglip-model', default='ViT-SO400M-14-SigLIP-384')
    parser.add_argument('--device', default='auto', help='auto | cuda | mps | cpu')
    parser.add_argument('--batch', type=int, default=16, help='photos fetched per round')
    parser.add_argument('--interval', type=int, default=120, help='seconds to wait when nothing is pending')
    parser.add_argument('--once', action='store_true', help='one pass, then exit')
    parser.add_argument('--ffmpeg', default=os.environ.get('FFMPEG', 'ffmpeg'), help='transcode: the ffmpeg binary')
    parser.add_argument('--ffprobe', default=os.environ.get('FFPROBE', 'ffprobe'), help='transcode: the ffprobe binary')
    parser.add_argument('--encoder', default='auto', help='transcode: auto | h264_nvenc | h264_videotoolbox | libx264')
    args = parser.parse_args()
    if args.command == 'login':
        Api(args.api).login()
    elif args.command == 'status':
        api = Api(args.api)
        print(json.dumps({'photos': api.stats(''), 'videos': api.request('GET', '/api/ai/videos/stats/').json()}, indent=2))
    elif args.command == 'transcode':
        from .transcode import run_transcode

        run_transcode(Api(args.api), args)
    else:
        run(args)


if __name__ == '__main__':
    main()
