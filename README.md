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
pip install -e .            # Mac
pip install -e '.[cuda]'    # NVIDIA: adds bitsandbytes for 4-bit weights
a2-photo-indexer login --api https://contracts.a2cons.com      # email, password, MFA code
```

**Memory.** Qwen3-VL-8B is ~16 GB of weights at full precision. The Mac
holds it whole in unified memory. On an NVIDIA card under 20 GB (the
5060 Ti's 16) `--quantize auto` (the default) loads it 4-bit (NF4,
~5.5 GB, everything stays on the GPU at full speed; captions are not
noticeably different -- a point or two on benchmarks). `--quantize
none|4bit|8bit` overrides. The precision is not part of the index's
model name: the Mac at full precision and the box at 4-bit work one
queue, and a photo either of them captioned is done for both.

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
name to retry). Ctrl-C stops cleanly after the current photo. A server
that is deploying or unreachable is waited out (30 s retries, leased
rows handed back), never a crash and never an "indexing failed" mark.
`A2_API=https://...` in the environment saves the `--api` flag.

## Plan sheets: reading title blocks

The same `run` also names plan sheets the app's text-layer detection
couldn't (scanned prints, title blocks drawn as outlines): whenever no
photo is pending it asks `/api/ai/plans/pending/`, shows the Qwen model
the two views the server renders (whole page + bottom-right quarter,
the same ones the Gemini fallback gets) with the server's own prompt,
and posts `{sheet_number, sheet_title}` back. Only sheets with a blank
number or the import placeholder title ("<file> p12") are queued, and
only those fields are filled — a name a person typed is never touched.
"Detect names" in the app on a sheet puts it back in this queue once.
Needs `--captioner qwen` (Florence takes no free prompt); `--no-plans`
runs photos only. `status` shows `plans: {total, pending, read_by_ai}`.

## Videos

The same worker also encodes videos (the server only probes a clip and
takes a poster). `run` does them as part of its rounds -- photos first,
then plan sheets, then one batch of videos, and straight back to the
top so a new photo never waits behind a long encode (`--no-videos`
opts out; ffmpeg drives the hardware video encoder, so the models stay
loaded). `a2-photo-indexer transcode --api … [--once]` does videos
alone; either way `--once` means "until the queue is empty", not one
batch. Per the owner's design:

- a **short clip** (up to the server's `VIDEO_SHORT_SECONDS`, 3 min) → one
  H.264 MP4, capped at 1080p, for browsers that can't decode the iPhone's
  HEVC (iOS/Safari keep playing the original by byte range);
- a **long video** → an HLS ladder (1080p / 720p / 480p, capped at the
  source), 6-second segments, uploaded as a ZIP of the folder.

HDR sources (PQ/HLG) are tone-mapped to SDR — needs an ffmpeg with the
`zscale` filter (libzimg): Homebrew's and Ubuntu's `ffmpeg` have it, some
static builds don't (the tool warns and encodes flat). The encoder is
picked automatically: NVENC on an NVIDIA box, VideoToolbox on a Mac,
x264 otherwise (`--encoder h264_nvenc|h264_videotoolbox|libx264`).
Uploads are resumable (tus, 8 MB chunks). Run it the same way as `run`
— a second unit/agent/task with `transcode` instead of `run`, or one
after the other in a script:

```sh
a2-photo-indexer run --once && a2-photo-indexer transcode --once
```

`status` shows both queues.

## Two machines at once

Run `run` (and/or `transcode`) on the Mac and the GPU box at the same
time: the app leases each row it hands out to the worker that asked
(named by `A2_WORKER` in the environment, else the hostname) for 30
minutes (3 hours for a video), so the other machine gets the next rows
instead of the same ones. A worker that dies mid-batch just lets its
leases expire and those photos are handed out again; `status` shows
`in_progress` = rows currently with a worker. Nothing to configure --
start both. Ctrl-C is clean: the photo being captioned finishes and is
posted, the rest of the batch is handed back to the queue at once
(only a crash or a lost network leaves rows to expire).

## Running on a schedule

Two ways: keep `a2-photo-indexer run` alive (it polls every 2 minutes,
`--interval`), or fire `run --once` every so often -- the better fit for
a machine that sleeps, since every run processes whatever piled up and
exits. Both need the one-time `login` on that machine first.

### Linux GPU box -- systemd user unit (always on)

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

```sh
systemctl --user daemon-reload
systemctl --user enable --now a2-photo-indexer
loginctl enable-linger $USER          # keep it running with nobody logged in
journalctl --user -u a2-photo-indexer -f
```

### macOS -- launchd agent (every 30 minutes while the Mac is awake)

`~/Library/LaunchAgents/com.a2cons.photo-indexer.plist` (adjust the two
paths to where the repo is; `StartInterval` is seconds):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.a2cons.photo-indexer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/cawka/Devel/a2contracts-photo-indexer/.venv/bin/a2-photo-indexer</string>
    <string>run</string>
    <string>--once</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>A2_API</key><string>https://contracts.a2cons.com</string></dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/cawka/Library/Logs/a2-photo-indexer.log</string>
  <key>StandardErrorPath</key><string>/Users/cawka/Library/Logs/a2-photo-indexer.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.a2cons.photo-indexer.plist   # install + start
launchctl kickstart -k gui/$(id -u)/com.a2cons.photo-indexer                              # run it now
tail -f ~/Library/Logs/a2-photo-indexer.log
launchctl bootout gui/$(id -u)/com.a2cons.photo-indexer                                   # remove
```

launchd skips the interval while the Mac sleeps and runs a missed job
on wake, so a laptop simply catches up when it is opened. Two runs never
overlap (launchd starts the next only after the previous exited). The
first run downloads the models (a few GB) -- do that one from a terminal
with `a2-photo-indexer run --once` so you can watch it.

### Windows 11 -- Task Scheduler (every 30 minutes)

Install (PowerShell, in the cloned repo):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128   # NVIDIA
pip install -e .[cuda]
setx A2_API https://contracts.a2cons.com                                             # for future shells and the task
a2-photo-indexer login
a2-photo-indexer run --once                                                          # first run: model download
```

The token file is `%USERPROFILE%\.a2contracts\photo-indexer.json`. Then
schedule it (one line, PowerShell, adjust the path):

```powershell
schtasks /Create /TN "A2 photo indexer" /SC MINUTE /MO 30 /F `
  /TR "\"$env:USERPROFILE\a2contracts-photo-indexer\.venv\Scripts\a2-photo-indexer.exe\" run --once --api https://contracts.a2cons.com"
schtasks /Run /TN "A2 photo indexer"       # run it now
schtasks /Query /TN "A2 photo indexer" /V /FO LIST | findstr /C:"Last Run" /C:"Last Result"
schtasks /Delete /TN "A2 photo indexer" /F  # remove
```

Or in the Task Scheduler app: Create Task → Triggers: "Daily, repeat
every 30 minutes for a duration of 1 day" → Actions: the
`.venv\Scripts\a2-photo-indexer.exe` above with arguments
`run --once --api https://contracts.a2cons.com` → Settings: tick "Run
task as soon as possible after a scheduled start is missed" and "Do not
start a new instance" (so a long pass is never doubled). Leave "Run only
when user is logged on" unless you also want it while signed out (then
it asks for your Windows password and runs without a console). Output
goes nowhere by default: add `>> %USERPROFILE%\a2-photo-indexer.log 2>&1`
to the arguments through `cmd /c "... "` if you want a log.

## What the app does with it

Videos: the lightbox plays the HLS ladder (hls.js, or natively on
iOS/Safari), else the original when the browser can decode it, else the
MP4 rendition; the iOS app plays the original or the HLS master with
AVPlayer.

- Photos tab search box: your captions + AI captions + tags, stemmed
  ("panels" finds "electrical panel"); staff see the AI caption and tags
  in the lightbox's Details.
- Lightbox → "Similar": the closest photos in the project by embedding.
- Album grantees never see AI fields; their search covers their albums.

API (company admin, bearer token): `GET /api/ai/photos/pending/?model=&limit=`,
`POST /api/ai/photos/<id>/index/`, `GET /api/ai/photos/stats/?model=`; plans:
`GET /api/ai/plans/pending/?limit=&worker=` → `{prompt, sheets: [{id, page,
views_url, …}]}`, `GET /api/ai/plans/<id>/views/` → `{full, corner}` (base64
JPEG), `POST /api/ai/plans/<id>/detection/` `{sheet_number, sheet_title,
model}`, `GET /api/ai/plans/stats/` — see the app repo,
`backend/estimating/photo_ai_views.py`.
