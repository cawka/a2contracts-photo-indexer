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
pip install -e '.[ocr]'         # Mac
pip install -e '.[cuda,ocr]'    # NVIDIA: adds bitsandbytes for 4-bit weights
brew install ffmpeg             # or: apt install ffmpeg -- video renditions
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

**Scanned sheets** have no text layer at all, so the app's "search
inside the pages" can't read them either. With the `[ocr]` extra
(RapidOCR: ONNX, CPU, no system packages; `rapidocr-onnxruntime` up to
Python 3.12, its successor `rapidocr` on 3.13+) the same `run` also works through
`/api/ai/plans/ocr/pending/`: it fetches the page image the server
renders, OCRs it, and posts every word with its box, which is what the
plan viewer highlights. Without that extra the queue is skipped with a
one-line notice; `--no-ocr` skips it deliberately. `status` shows
`plan_ocr: {total, pending, ocr_done}`.

## Daily reports: writing the note

A project's daily report in the app has a note at the top. When the
project manager presses "Write note" on the draft, the app queues it at
`/api/ai/reports/pending/`; `run` picks it up right after photos (within
the usual idle interval) and gives the
Qwen model the server's prompt, the draft as plain text (photos with what
this indexer already saw in them, permit/inspection changes, new plan
sheets, 3D models) and up to four of the day's photos, and posts the note
back to `/api/ai/reports/<id>/suggestion/`. The app fills the draft's note
with it unless a person already wrote one (then it's offered as a
suggestion). Needs `--captioner qwen`; `--no-reports` skips it.

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
  source), 6-second segments, uploaded as a ZIP of the folder;
- then, for every video, a **loop clip**: 3 silent seconds, at most 640 px
  on the long side, for the looping tiles of the app's daily reports
  (tens of KB; the worker asks for these with `loops=1`, so an older
  worker is never handed one).

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

Two modes, ready-made for each OS in `macOS/`, `linux/` and `win11/`:

- **interval** -- `run --once` every 30 minutes: loads the models, drains
  the queue, exits. Memory is free between runs; new photos wait up to
  30 minutes. The better fit for a machine that sleeps.
- **daemon** -- `run` always on: the models stay loaded (~16 GB with the
  default Qwen captioner) and new photos are picked up within
  `--interval` seconds (default 120). A server that is down or deploying
  is retried every 30s in-process.

Each folder's install script fills in where the repo is, replaces
whatever mode was installed before, and starts it. Both modes need the
one-time `login` on that machine first, and the first run downloads the
models (a few GB) -- do that one from a terminal with
`a2-photo-indexer run --once` so you can watch it.

### macOS -- launchd agent

```sh
macOS/install.sh interval        # or: daemon
tail -f ~/Library/Logs/a2-photo-indexer.log
launchctl kickstart -k gui/$(id -u)/com.a2cons.photo-indexer   # run / restart it now
macOS/install.sh remove
```

It writes `~/Library/LaunchAgents/com.a2cons.photo-indexer.plist` from
`macOS/com.a2cons.photo-indexer.{interval,daemon}.plist`. launchd reads
the plist only when it is loaded, so after editing one, run the install
script again. The plists set `PATH` to include `/opt/homebrew/bin`:
launchd starts jobs with a bare `/usr/bin:/bin:...`, and without it
Homebrew's ffmpeg is not found and videos wait.

Interval: launchd skips the interval while the Mac sleeps and runs a
missed job on wake, and never starts a run while the previous one is
still going. Daemon: `KeepAlive` restarts the process whenever it exits
(at most once a minute, so a crash at startup doesn't reload the models
in a loop) -- stop it with `install.sh remove`, not `kill`. While the
Mac sleeps the process is frozen and carries on after wake.

### Linux GPU box -- systemd user unit

```sh
linux/install.sh daemon          # or: interval
journalctl --user -u a2-photo-indexer -f          # daemon
journalctl --user -u a2-photo-indexer-once -f     # interval
systemctl --user restart a2-photo-indexer         # restart (daemon)
systemctl --user start a2-photo-indexer-once      # run a pass now (interval)
linux/install.sh remove
```

Daemon is `linux/a2-photo-indexer.service` (`Restart=always`); interval
is `linux/a2-photo-indexer-once.service` started by
`a2-photo-indexer-once.timer` (30 minutes after the previous pass ended;
`Persistent=true` catches up after downtime). The script also runs
`loginctl enable-linger` so it keeps running with nobody logged in.

### Windows 11 -- Task Scheduler

Install (PowerShell, in the cloned repo):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128   # NVIDIA
pip install -e .[cuda,ocr]
a2-photo-indexer login
a2-photo-indexer run --once                                                          # first run: model download
```

The token file is `%USERPROFILE%\.a2contracts\photo-indexer.json`. Then:

```powershell
powershell -ExecutionPolicy Bypass -File win11\install.ps1 interval   # or: daemon
Get-Content -Wait $env:USERPROFILE\a2-photo-indexer.log
Start-ScheduledTask "A2 photo indexer"                                  # run it now
powershell -ExecutionPolicy Bypass -File win11\install.ps1 remove
```

(Restarting on Windows needs one more step -- see below.)

It registers the task "A2 photo indexer" for your user, logging to
`%USERPROFILE%\a2-photo-indexer.log`. Interval: every 30 minutes, a
missed run starts as soon as possible, and a long pass is never doubled.
Daemon: starts at logon and is restarted a minute after it exits. Both
run only while you are signed in; for signed-out runs, open the task in
the Task Scheduler app and pick "Run whether user is logged on or not".

## Updating, restarting, stopping

**After pulling a new version** (`git pull` in the repo): the package is
installed with `pip install -e`, so the new code is used as soon as the
process starts again -- nothing to reinstall. Run `pip install -e .`
(with the same extras as at setup, e.g. `.[ocr]` / `.[cuda,ocr]`) only
when `pyproject.toml` changed (a new dependency). Then:

- **interval** mode: nothing to do -- the next pass (within 30 minutes)
  starts the new code. The "run now" commands below start one at once.
- **daemon** mode: the running process keeps the OLD code (and the
  loaded models) until it is restarted -- restart it.

A restart unloads the models; the next start loads them again (a minute
or two with Qwen, no download). Anything the old process had taken from
the server's queues but not finished goes back to the queue when its
lease runs out, so nothing is lost.

**macOS** (launchd):

```sh
launchctl kickstart -k gui/$(id -u)/com.a2cons.photo-indexer   # restart now (daemon), or run a pass now (interval)
launchctl bootout gui/$(id -u)/com.a2cons.photo-indexer        # stop until you start it again...
macOS/install.sh daemon                                        # ...with this (or: interval)
launchctl print gui/$(id -u)/com.a2cons.photo-indexer | grep -E 'state|pid'   # is it running?
```

`kickstart -k` kills the running process and starts it again right away.
Don't `kill` the daemon yourself: `KeepAlive` just starts it again.
`bootout` is the stop that sticks (until the next `install.sh` or a
reboot -- the agent loads again at login).

**Linux** (systemd user unit):

```sh
systemctl --user restart a2-photo-indexer         # restart (daemon)
systemctl --user start a2-photo-indexer-once      # run a pass now (interval)
systemctl --user stop a2-photo-indexer            # stop until the next start/reboot (daemon)
systemctl --user stop a2-photo-indexer-once.timer # pause the 30-minute passes (interval)
systemctl --user status a2-photo-indexer          # is it running?
```

**Windows 11** (Task Scheduler): stopping the task ends its `cmd.exe`,
but not always the indexer process under it, so stop that explicitly:

```powershell
Stop-ScheduledTask "A2 photo indexer"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*a2-photo-indexer*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask "A2 photo indexer"                   # start again (skip this line to just stop)
Get-ScheduledTask "A2 photo indexer" | Select-Object State   # Running / Ready
```

A daemon-mode task stopped this way stays stopped until the next logon
or `Start-ScheduledTask`.

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
