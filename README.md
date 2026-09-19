# A2 Contracts photo indexer

The AI half of the Photos feature (the app repo's `docs/photos-design.md`, phase 3). The
server keeps a queue of photos with no AI index; this script runs the
models on **your** machine, whenever it is on, and posts back a caption,
tags and an image embedding per photo. The app then searches captions +
tags (Postgres full-text) and finds "similar photos" (embeddings) — no
model ever runs on the server, and no photo leaves your machines.

It is a plain Python script talking to the REST API with a device token
(the same auth as the iOS app and the MCP server), so it runs anywhere
with a GPU (the 5060 Ti box: CUDA) or Apple silicon (the M3: MPS).

## Models

| Piece | Default | Notes |
|---|---|---|
| Caption + tags | `Qwen/Qwen3-VL-8B-Instruct` | one prompt → JSON `{caption, tags}`. 8B needs ~16 GB in fp16: on the 5060 Ti use `--qwen-model Qwen/Qwen3-VL-4B-Instruct` or a 4-bit build; the M3 (96 GB) runs 8B or 32B. `--captioner florence` (`microsoft/Florence-2-large`, 0.8B) is the light fallback, `--captioner none` skips captions. |
| Embedding + vocabulary tags | `ViT-SO400M-14-SigLIP-384` (open_clip, `webli` weights) | 1152-dim vector for "similar photos", plus zero-shot tags from the construction vocabulary in `indexer.py` (`VOCABULARY`) — consistent tags across photos, unlike free text. `--embedder none` skips it. |

The index's **model name** is `<captioner>+<embedder>` (e.g.
`Qwen3-VL-8B-Instruct+ViT-SO400M-14-SigLIP-384`). Change a model and every
photo is pending again for the new name; the server keeps the old rows
until they are replaced, so search keeps working through a re-index.

## Set up (once per machine)

Its own repo, nothing from the app repo needed:

```sh
git clone git@github.com:cawka/a2contracts-photo-indexer.git
cd a2contracts-photo-indexer
python3 -m venv .venv && source .venv/bin/activate
# torch for your hardware -- pick one:
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128   # NVIDIA (CUDA 12.8)
pip install torch torchvision                                                       # Apple silicon (MPS) / CPU
pip install -e .
a2-photo-indexer login --api https://contracts.a2cons.com      # email, password, MFA code
```

`login` must be a **company admin** account (the indexer reads every photo
of the company). The token pair lands in `~/.a2contracts/photo-indexer.json`
(mode 600) and refreshes itself; it shows up under Profile → Signed-in
devices as `photo-indexer@<hostname>`, revocable there. Models download
from Hugging Face on first run (~2 GB for 4B/8B Qwen + ~1.5 GB SigLIP).

## Run

```sh
a2-photo-indexer status --api https://contracts.a2cons.com           # {total, indexed, pending}
a2-photo-indexer run --api https://contracts.a2cons.com --once       # one pass over what's pending
a2-photo-indexer run --api https://contracts.a2cons.com              # keep going; polls every 2 min
a2-photo-indexer run --captioner qwen --qwen-model Qwen/Qwen3-VL-4B-Instruct --batch 32
```

`--device auto` picks cuda → mps → cpu. Each photo prints its time and
caption; a photo whose models fail is recorded with the tag
`indexing failed` so the queue moves on (re-run with a different model
name to retry). Ctrl-C stops cleanly after the current photo.
`A2_API=https://...` in the environment saves the `--api` flag.

### Unattended on the GPU box (systemd user unit)

```ini
# ~/.config/systemd/user/a2-photo-indexer.service
[Unit]
Description=A2 Contracts photo indexer
After=network-online.target

[Service]
Environment=A2_API=https://contracts.a2cons.com
ExecStart=%h/a2contracts-photo-indexer/.venv/bin/a2-photo-indexer run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

`systemctl --user enable --now a2-photo-indexer` (and `loginctl
enable-linger $USER` so it runs without a login session). On the Mac,
`a2-photo-indexer run --once` from a terminal, or a `launchd` agent with
the same command, does the same job whenever the laptop is open.

## What the app does with it

- Photos tab search box: your captions + AI captions + tags, stemmed
  ("panels" finds "electrical panel"); staff see the AI caption and tags
  in the lightbox's Details.
- Lightbox → "Similar": the closest photos in the project by embedding.
- Album grantees never see AI fields; their search covers their albums.

API (company admin, bearer token): `GET /api/ai/photos/pending/?model=&limit=`,
`POST /api/ai/photos/<id>/index/`, `GET /api/ai/photos/stats/?model=` — see
the app repo, `backend/estimating/photo_ai_views.py`.
