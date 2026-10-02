#!/usr/bin/env python3
"""
Video Subtitle Burner - Web app for Railway
Burns Burmese/English subtitles into videos using ffmpeg.

Features:
- Download video from remote URL
- Upload VTT/SRT subtitle file
- Background encoding (survives browser close)
- Download link when done, auto-cleanup
"""
import os
import re
import uuid
import json
import time
import threading
import subprocess
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, request, jsonify, render_template, send_file

app = Flask(__name__)

# Config
BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

# Fonts (bundled or system)
FONT_MM = os.environ.get("FONT_MM", "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf")
FONT_LAT = os.environ.get("FONT_LAT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Cleanup after 24 hours
JOB_TTL_HOURS = 24


def parse_subtitles(path):
    """Parse VTT or SRT, return list of (start_sec, end_sec, text)."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    cues = []
    is_vtt = content.strip().startswith("WEBVTT")

    if is_vtt:
        # VTT: 00:00:20.861 --> 00:00:21.121
        pattern = r'(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})\n(.*?)(?=\n\n|\Z)'
    else:
        # SRT: 00:00:20,861 --> 00:00:21,121
        pattern = r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\n(.*?)(?=\n\n|\Z)'

    for m in re.finditer(pattern, content, re.DOTALL):
        h1, mi1, s1, ms1, h2, mi2, s2, ms2, text = m.groups()
        start = int(h1)*3600 + int(mi1)*60 + int(s1) + int(ms1)/1000
        end = int(h2)*3600 + int(mi2)*60 + int(s2) + int(ms2)/1000
        # Clean text: remove tags, join lines
        t = re.sub(r'<[^>]+>', '', text).strip().replace('\n', ' ')
        if t:
            cues.append((start, end, t))

    return cues


def render_subtitle_png(text, out_path, fontsize=36):
    """
    Render subtitle as PNG with per-script font selection.
    - Myanmar (U+1000-U+109F): NotoSansMyanmar-Bold
    - Latin/digits: DejaVuSans-Bold
    - Black semi-transparent box, white text
    - Position handled by ffmpeg overlay (bottom, standard cinema position)
    """
    from PIL import Image, ImageDraw, ImageFont

    SCALE = 2
    fs = fontsize * SCALE

    font_mm = ImageFont.truetype(FONT_MM, fs, layout_engine=ImageFont.Layout.RAQM)
    font_lat = ImageFont.truetype(FONT_LAT, fs, layout_engine=ImageFont.Layout.RAQM)

    def get_font(ch):
        if '\u1000' <= ch <= '\u109f':
            return font_mm
        return font_lat

    # Split text into runs by script for proper font rendering
    runs = []
    current_run = ""
    current_font = None
    for ch in text:
        f = get_font(ch)
        if f != current_font and current_run:
            runs.append((current_run, current_font))
            current_run = ""
        current_font = f
        current_run += ch
    if current_run:
        runs.append((current_run, current_font))

    # Measure total width
    tmp_img = Image.new('RGBA', (10, 10))
    tmp_d = ImageDraw.Draw(tmp_img)
    total_w = 0
    max_h = 0
    run_widths = []
    for run_text, run_font in runs:
        bbox = tmp_d.textbbox((0, 0), run_text, font=run_font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        run_widths.append((w, bbox))
        total_w += w
        max_h = max(max_h, h)

    pad_x, pad_y = 24 * SCALE, 14 * SCALE
    W = total_w + pad_x * 2
    H = max_h + pad_y * 2

    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Semi-transparent black box
    d.rectangle([0, 0, W-1, H-1], fill=(0, 0, 0, 160))

    # Draw each run
    x = pad_x
    for (run_text, run_font), (w, bbox) in zip(runs, run_widths):
        d.text((x - bbox[0], pad_y - bbox[1]), run_text, font=run_font, fill=(255, 255, 255, 255))
        x += w

    # Downscale to 1x for crispness
    img = img.resize((W // SCALE, H // SCALE), Image.LANCZOS)
    img.save(out_path)


def download_video(url, dest):
    """Download video from URL with progress."""
    r = requests.get(url, stream=True, timeout=30)
    r.raise_for_status()
    total = int(r.headers.get('content-length', 0))
    downloaded = 0
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
    return dest


def encode_job(job_id, video_url, sub_path, watermark_text=None):
    """Background encoding job."""
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    status_file = job_dir / "status.json"

    def set_status(**kwargs):
        s = {}
        if status_file.exists():
            s = json.loads(status_file.read_text())
        s.update(kwargs)
        s['updated'] = datetime.now().isoformat()
        status_file.write_text(json.dumps(s))

    try:
        set_status(status="downloading", progress=5)

        # 1. Download video
        video_path = job_dir / "input.mp4"
        download_video(video_url, video_path)
        set_status(status="parsing", progress=15)

        # 2. Parse subtitles
        cues = parse_subtitles(sub_path)
        if not cues:
            raise ValueError("No subtitles found in file")
        set_status(status="rendering", progress=20, total_cues=len(cues))

        # 3. Render PNGs
        png_dir = job_dir / "pngs"
        png_dir.mkdir(exist_ok=True)
        png_files = []
        for i, (start, end, text) in enumerate(cues):
            png_path = png_dir / f"sub_{i:04d}.png"
            render_subtitle_png(text, str(png_path))
            png_files.append((str(png_path), start, end))
            if i % 50 == 0:
                set_status(progress=20 + int(20 * i / len(cues)))

        set_status(status="encoding", progress=45)

        # 4. Build ffmpeg filter
        # Position: centered, 60px from bottom (standard cinema position)
        filter_parts = []
        prev = "[0:v]"
        for i, (png_path, start, end) in enumerate(png_files):
            idx = i + 1
            out = f"[s{i}]" if i < len(png_files) - 1 else "[vout]"
            filter_parts.append(
                f"{prev}[{idx}:v]overlay=x=(W-w)/2:y=H-h-60:"
                f"enable='between(t,{start:.3f},{end:.3f})'{out}"
            )
            prev = out

        # Watermark (optional)
        inputs = ["-i", str(video_path)]
        for png_path, _, _ in png_files:
            inputs += ["-i", png_path]

        output_path = job_dir / "output.mp4"

        # Get video info for quality matching
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate",
             "-of", "default=noprint_wrappers=1", str(video_path)],
            capture_output=True, text=True
        )

        # Encode: CRF 21 (near-original quality), preserve resolution
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "21", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ]

        # Run with progress monitoring
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                universal_newlines=True, bufsize=1)

        # Get duration for progress
        dur_proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True
        )
        try:
            duration = float(dur_proc.stdout.strip())
        except:
            duration = 1

        for line in proc.stderr:
            # Parse time= from ffmpeg output
            m = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
            if m:
                h, mi, s = m.groups()
                t = int(h)*3600 + int(mi)*60 + float(s)
                pct = 45 + int(50 * t / duration)
                set_status(progress=min(pct, 95))

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed with code {proc.returncode}")

        # Verify output
        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise RuntimeError("Output file not created or too small")

        set_status(status="done", progress=100,
                   output=str(output_path.name),
                   size=output_path.stat().st_size)

    except Exception as e:
        set_status(status="error", error=str(e))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/submit', methods=['POST'])
def submit():
    video_url = request.form.get('video_url', '').strip()
    if not video_url:
        return jsonify({"error": "Video URL required"}), 400

    if 'subtitle' not in request.files:
        return jsonify({"error": "Subtitle file required"}), 400

    sub_file = request.files['subtitle']
    if not sub_file.filename:
        return jsonify({"error": "No subtitle file selected"}), 400

    ext = Path(sub_file.filename).suffix.lower()
    if ext not in ['.vtt', '.srt']:
        return jsonify({"error": "Only VTT or SRT files supported"}), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    sub_path = job_dir / f"sub{ext}"
    sub_file.save(str(sub_path))

    # Start background encoding
    thread = threading.Thread(
        target=encode_job,
        args=(job_id, video_url, str(sub_path)),
        daemon=True
    )
    thread.start()

    (job_dir / "status.json").write_text(json.dumps({
        "status": "queued", "progress": 0,
        "created": datetime.now().isoformat()
    }))

    return jsonify({"job_id": job_id})


@app.route('/api/status/<job_id>')
def status(job_id):
    status_file = JOBS_DIR / job_id / "status.json"
    if not status_file.exists():
        return jsonify({"error": "Job not found"}), 404
    return jsonify(json.loads(status_file.read_text()))


@app.route('/api/download/<job_id>')
def download(job_id):
    job_dir = JOBS_DIR / job_id
    output = job_dir / "output.mp4"
    if not output.exists():
        return jsonify({"error": "File not ready"}), 404
    return send_file(str(output), as_attachment=True,
                     download_name="subtitled.mp4",
                     mimetype="video/mp4")


@app.route('/api/cleanup', methods=['POST'])
def cleanup():
    """Remove old jobs."""
    now = datetime.now()
    removed = 0
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        status_file = job_dir / "status.json"
        if status_file.exists():
            try:
                s = json.loads(status_file.read_text())
                created = datetime.fromisoformat(s.get('created', ''))
                if now - created > timedelta(hours=JOB_TTL_HOURS):
                    shutil.rmtree(job_dir)
                    removed += 1
            except:
                pass
    return jsonify({"removed": removed})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
