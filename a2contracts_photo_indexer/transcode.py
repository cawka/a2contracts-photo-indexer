"""`a2-photo-indexer transcode`: the video half of the worker. The app's
server only probes a clip and takes a poster; every playable encoding
comes from here (the owner: encoding is "deferred to powerful worker",
the server "just one in super low quality... or nothing at all").

For each pending video (GET /api/ai/videos/pending/):
  - `wanted: mp4`  (a short clip, up to the server's VIDEO_SHORT_SECONDS)
    -> one H.264 MP4, capped at 1080p, `faststart`, for browsers that
       can't decode the iPhone's HEVC (Safari/iOS play the original).
  - `wanted: hls`  (a long video) -> an HLS ladder (1080p / 720p / 480p,
       capped at the source), 6-second segments, one master playlist,
       uploaded as a ZIP of the folder.
An HDR source (PQ/HLG) is tone-mapped to SDR BT.709 -- the same color
trap as the stills; without it the picture is washed out. Needs an
ffmpeg with `zscale` (libzimg) for that: Homebrew's and Ubuntu's have
it; the encoder is NVENC on an NVIDIA box, VideoToolbox on a Mac, x264
otherwise (`--encoder` to force one). Uploads go through the app's tus
endpoint in 8 MB chunks, so a dropped connection resumes.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

CHUNK = 8 * 1024 * 1024
LADDER = [(1920, 1080, '6000k'), (1280, 720, '3000k'), (854, 480, '1200k')]


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def ffmpeg_has(binary: str, what: str, name: str) -> bool:
    out = _run([binary, '-hide_banner', f'-{what}'], timeout=30).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def pick_encoder(ffmpeg: str, requested: str) -> tuple[str, list[str]]:
    """(encoder name, its quality flags)."""
    if requested != 'auto':
        name = requested
    elif ffmpeg_has(ffmpeg, 'encoders', 'h264_nvenc'):
        name = 'h264_nvenc'
    elif ffmpeg_has(ffmpeg, 'encoders', 'h264_videotoolbox'):
        name = 'h264_videotoolbox'
    else:
        name = 'libx264'
    flags = {
        'h264_nvenc': ['-preset', 'p5', '-rc', 'vbr', '-cq', '23', '-b:v', '0'],
        'h264_videotoolbox': ['-q:v', '60', '-allow_sw', '1'],
        'libx264': ['-preset', 'medium', '-crf', '22'],
    }.get(name, ['-crf', '22'])
    return name, flags


def probe(ffprobe: str, path: str) -> dict:
    out = _run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path], timeout=120)
    data = json.loads(out.stdout or '{}')
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
    audio = any(s.get('codec_type') == 'audio' for s in data.get('streams', []))
    w, h = int(video.get('width') or 0), int(video.get('height') or 0)
    rot = 0
    for side in video.get('side_data_list') or []:
        if side.get('rotation') is not None:
            rot = int(float(side['rotation']))
    if rot % 180:
        w, h = h, w
    transfer = (video.get('color_transfer') or '').lower()
    return {'width': w, 'height': h, 'hdr': transfer in ('smpte2084', 'arib-std-b67'), 'audio': audio, 'duration': float((data.get('format') or {}).get('duration') or 0)}


def video_filter(ffmpeg: str, hdr: bool, target_h: int | None) -> str:
    parts = []
    if hdr:
        if not ffmpeg_has(ffmpeg, 'filters', 'zscale'):
            print('warning: this ffmpeg has no zscale -- HDR will come out flat; install an ffmpeg with libzimg', file=sys.stderr)
        else:
            parts.append('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv')
    if target_h:
        parts.append(f"scale='min(iw,-2)':'min(ih,{target_h})':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2")
    parts.append('format=yuv420p')
    return ','.join(parts)


def encode_mp4(ffmpeg: str, ffprobe: str, src: str, out: str, encoder: str) -> dict:
    info = probe(ffprobe, src)
    target_h = min(1080, info['height']) if info['height'] else None
    name, flags = pick_encoder(ffmpeg, encoder)
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', src, '-map', '0:v:0']
    if info['audio']:
        cmd += ['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-ac', '2']
    cmd += ['-vf', video_filter(ffmpeg, info['hdr'], target_h), '-c:v', name, *flags, '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-map_metadata', '-1', out]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr.strip()[-400:]}')
    done = probe(ffprobe, out)
    return {'width': done['width'], 'height': done['height'], 'encoder': name}


def encode_hls(ffmpeg: str, ffprobe: str, src: str, folder: str, encoder: str) -> dict:
    info = probe(ffprobe, src)
    name, flags = pick_encoder(ffmpeg, encoder)
    rungs = [(w, h, br) for w, h, br in LADDER if not info['height'] or h <= info['height']] or [LADDER[-1]]
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', src]
    filter_complex = f"[0:v]{video_filter(ffmpeg, info['hdr'], None)},split={len(rungs)}" + ''.join(f'[v{i}]' for i in range(len(rungs))) + ';'
    filter_complex += ';'.join(f"[v{i}]scale='min(iw,-2)':'min(ih,{h})':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2[o{i}]" for i, (_, h, _) in enumerate(rungs))
    cmd += ['-filter_complex', filter_complex]
    stream_map = []
    for i, (_, _, bitrate) in enumerate(rungs):
        cmd += ['-map', f'[o{i}]', f'-c:v:{i}', name, *flags, f'-maxrate:v:{i}', bitrate, f'-bufsize:v:{i}', str(int(bitrate[:-1]) * 2) + 'k', f'-g:v:{i}', '60', f'-keyint_min:v:{i}', '60', f'-sc_threshold:v:{i}', '0']
        if info['audio']:
            cmd += ['-map', '0:a:0?', f'-c:a:{i}', 'aac', f'-b:a:{i}', '128k', f'-ac:a:{i}', '2']
            stream_map.append(f'v:{i},a:{i},name:{rungs[i][1]}p')
        else:
            stream_map.append(f'v:{i},name:{rungs[i][1]}p')
    cmd += [
        '-f', 'hls', '-hls_time', '6', '-hls_playlist_type', 'vod', '-hls_flags', 'independent_segments', '-hls_segment_type', 'mpegts',
        '-master_pl_name', 'master.m3u8', '-var_stream_map', ' '.join(stream_map),
        '-hls_segment_filename', os.path.join(folder, '%v_%04d.ts'), os.path.join(folder, '%v.m3u8'),
    ]
    result = _run(cmd, timeout=6 * 3600)
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr.strip()[-400:]}')
    top = rungs[0]
    height = min(top[1], info['height'] or top[1])
    width = int(round(height * (info['width'] / info['height']) / 2) * 2) if info['width'] and info['height'] else top[0]
    return {'width': width, 'height': height, 'encoder': name}


def zip_folder(folder: str, out: str) -> None:
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as z:
        for name in sorted(os.listdir(folder)):
            z.write(os.path.join(folder, name), name)


# --- Resumable upload (tus) ---------------------------------------------------------

def tus_upload(api, path: str, metadata: dict) -> dict:
    size = os.path.getsize(path)
    meta = ','.join(f'{k} {base64.b64encode(str(v).encode()).decode()}' for k, v in metadata.items())
    r = api.request('POST', '/api/uploads/', headers_extra={'Tus-Resumable': '1.0.0', 'Upload-Length': str(size), 'Upload-Metadata': meta})
    if r.status_code != 201:
        raise RuntimeError(f'tus create failed ({r.status_code}): {r.text[:300]}')
    location = r.headers['Location']
    url_path = location[location.index('/api/'):]
    offset = 0
    with open(path, 'rb') as f:
        while offset < size:
            f.seek(offset)
            chunk = f.read(CHUNK)
            r = api.request('PATCH', url_path, data=chunk, headers_extra={'Tus-Resumable': '1.0.0', 'Upload-Offset': str(offset), 'Content-Type': 'application/offset+octet-stream'})
            if r.status_code == 409:
                offset = int(r.headers.get('Upload-Offset', offset))
                continue
            if r.status_code != 204:
                raise RuntimeError(f'tus chunk failed ({r.status_code}): {r.text[:300]}')
            offset = int(r.headers['Upload-Offset'])
    status = api.request('GET', url_path).json()
    if status.get('error'):
        raise RuntimeError(status['error'])
    return status.get('result') or {}


def download(api, url: str, dest: str) -> None:
    r = api.request('GET', url[len(api.base):] if url.startswith(api.base) else url, stream=True, timeout=600)
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(1024 * 1024):
            f.write(chunk)


def run_transcode(api, args) -> None:
    ffmpeg, ffprobe = args.ffmpeg, args.ffprobe
    if not shutil.which(ffmpeg) or not shutil.which(ffprobe):
        sys.exit('ffmpeg/ffprobe not found on the PATH (brew install ffmpeg / apt install ffmpeg).')
    print(f'encoder: {pick_encoder(ffmpeg, args.encoder)[0]}')
    stop = {'now': False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__('now', True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__('now', True))
    while not stop['now']:
        from .indexer import WORKER

        r = api.request('GET', f'/api/ai/videos/pending/?limit={args.batch}&worker={WORKER}')
        r.raise_for_status()
        pending = r.json()
        stats = api.request('GET', '/api/ai/videos/stats/').json()
        if not pending:
            busy = f', {stats["in_progress"]} with other workers' if stats.get('in_progress') else ''
            print(f'nothing pending ({stats["encoded"]}/{stats["total"]} videos encoded{busy})')
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
                api.release('transcode', [r['id'] for r in pending[n:]])
                return
            started = time.time()
            with tempfile.TemporaryDirectory(prefix='a2-video-') as tmp:
                try:
                    src = os.path.join(tmp, 'source.bin')
                    download(api, row['url'], src)
                    if row['wanted'] == 'mp4':
                        out = os.path.join(tmp, 'out.mp4')
                        done = encode_mp4(ffmpeg, ffprobe, src, out, args.encoder)
                        meta = {'filename': f"{row['id']}-{done['height']}p.mp4", 'purpose': 'video-rendition', 'photo': row['id'], 'kind': 'mp4', 'label': f"{done['height']}p", 'width': done['width'], 'height': done['height'], 'encoder': done['encoder']}
                        tus_upload(api, out, meta)
                    else:
                        folder = os.path.join(tmp, 'hls')
                        os.makedirs(folder)
                        done = encode_hls(ffmpeg, ffprobe, src, folder, args.encoder)
                        out = os.path.join(tmp, 'hls.zip')
                        zip_folder(folder, out)
                        meta = {'filename': f"{row['id']}-hls.zip", 'purpose': 'video-rendition', 'photo': row['id'], 'kind': 'hls', 'label': 'hls', 'width': done['width'], 'height': done['height'], 'encoder': done['encoder']}
                        tus_upload(api, out, meta)
                    print(f"#{row['id']} {row['project_name'][:30]:30s} {row['wanted']} {time.time() - started:6.1f}s  {done['width']}x{done['height']} {done['encoder']}")
                except Exception as exc:  # noqa: BLE001 -- one bad clip must not stop the run
                    print(f"#{row['id']} FAILED: {exc}", file=sys.stderr)
                    time.sleep(2)
        if args.once:
            return
