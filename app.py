#!/usr/bin/env python3
"""
Video Subtitle Burner for Railway.

Features:
- Downloads a remote video
- Accepts VTT/SRT subtitle uploads
- Converts subtitles to ASS
- Burns subtitles in one continuous FFmpeg encode
- Preserves browser job history through localStorage
- Supports Railway persistent volumes
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file


app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Railway volume ရှိရင် အဲဒီနေရာကိုသုံးမယ်။
# Volume မရှိရင် project ထဲက jobs folder ကိုသုံးမယ်။
_storage_path = (
    os.environ.get("JOBS_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
)

if _storage_path:
    JOBS_DIR = Path(_storage_path).resolve()
else:
    JOBS_DIR = (BASE_DIR / "jobs").resolve()

JOBS_DIR.mkdir(parents=True, exist_ok=True)

FONT_NAME = os.environ.get("SUBTITLE_FONT_NAME", "Noto Sans Myanmar")
FONTS_DIR = os.environ.get(
    "FONTS_DIR",
    "/usr/share/fonts/truetype/noto"
)

JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "24"))
MAX_HISTORY_IDS = 20
VALID_JOB_ID = re.compile(r"^[a-f0-9]{12}$")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def valid_job_id(job_id):
    return bool(VALID_JOB_ID.fullmatch(job_id or ""))


def read_status_file(status_file):
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path, data):
    """
    status API ကဖတ်နေချိန် status.json တစ်ဝက်တစ်ပျက်ဖြစ်မသွားအောင်
    temporary file ရေးပြီး atomic replace လုပ်သည်။
    """
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8"
    )
    os.replace(temp_path, path)


def update_status(job_id, **changes):
    job_dir = JOBS_DIR / job_id
    status_file = job_dir / "status.json"

    current = read_status_file(status_file) or {}
    current.update(changes)
    current["updated"] = now_iso()

    write_json_atomic(status_file, current)
    return current


def parse_timestamp(value):
    """
    Supports:
    00:01:23.456
    00:01:23,456
    01:23.456
    01:23,456
    """
    value = value.strip().replace(",", ".")
    parts = value.split(":")

    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        elif len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
        else:
            return None

        total = hours * 3600 + minutes * 60 + seconds
        return max(0.0, total)
    except ValueError:
        return None


def clean_subtitle_line(text):
    text = re.sub(r"<[^>]*>", "", text)
    text = html.unescape(text)
    return text.strip()


def parse_subtitles(path):
    """
    Parse VTT or SRT subtitles.

    Returns:
        [
            (start_seconds, end_seconds, ["line 1", "line 2"]),
            ...
        ]
    """
    content = Path(path).read_text(
        encoding="utf-8-sig",
        errors="replace"
    )

    content = content.replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n[ \t]*\n", content.strip())
    cues = []

    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines()]

        timing_index = None
        for index, line in enumerate(lines):
            if "-->" in line:
                timing_index = index
                break

        if timing_index is None:
            continue

        timing_line = lines[timing_index]
        left, right = timing_line.split("-->", 1)

        start_token = left.strip().split()[0] if left.strip() else ""
        right_parts = right.strip().split()
        end_token = right_parts[0] if right_parts else ""

        start = parse_timestamp(start_token)
        end = parse_timestamp(end_token)

        if start is None or end is None or end <= start:
            continue

        text_lines = []
        for line in lines[timing_index + 1:]:
            cleaned = clean_subtitle_line(line)
            if cleaned:
                text_lines.append(cleaned)

        if text_lines:
            cues.append((start, end, text_lines))

    cues.sort(key=lambda cue: cue[0])
    return cues


def ass_time(seconds):
    """
    Convert seconds to ASS timestamp:
    H:MM:SS.cc
    """
    seconds = max(0.0, float(seconds))
    total_centiseconds = int(round(seconds * 100))

    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)

    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def escape_ass_text(text):
    """
    Prevent subtitle text from injecting ASS formatting commands.
    """
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    return text


def create_ass_subtitle(cues, output_path):
    """
    Create a resolution-independent ASS subtitle file.

    PlayResY=1080 နဲ့ Fontsize=36 ဖြစ်တဲ့အတွက်:
    - 1080p မှာ 36
    - 720p မှာ 24 ဝန်းကျင်
    - 480p မှာ 16 ဝန်းကျင်

    ဒါကြောင့် မူရင်း fixed 36-pixel PNG ထက် ပုံမှန်အရွယ်ဖြစ်မယ်။
    """
    header = f"""[Script Info]
Title: Video Subtitle Burner
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},36,&H00FFFFFF,&H00FFFFFF,&H60000000,&H60000000,-1,0,0,0,100,100,0,0,3,4,0,2,60,60,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []

    for start, end, text_lines in cues:
        cleaned_lines = [
            escape_ass_text(line)
            for line in text_lines
            if line.strip()
        ]

        if not cleaned_lines:
            continue

        text = r"\N".join(cleaned_lines)

        events.append(
            "Dialogue: 0,"
            f"{ass_time(start)},"
            f"{ass_time(end)},"
            "Default,,0,0,0,,"
            f"{text}"
        )

    output_path.write_text(
        header + "\n".join(events) + "\n",
        encoding="utf-8"
    )


def download_video(url, destination):
    """
    Download remote video with connect/read timeout.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
    }

    with requests.get(
        url,
        stream=True,
        timeout=(20, 180),
        allow_redirects=True,
        headers=headers
    ) as response:
        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)

    if not destination.exists() or destination.stat().st_size < 1024:
        raise RuntimeError("Downloaded video file is empty or invalid")

    return destination


def probe_duration(video_path):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Cannot read video duration: "
            + (result.stderr.strip() or "ffprobe failed")
        )

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Invalid video duration") from exc

    if duration <= 0:
        raise RuntimeError("Video duration is zero or invalid")

    return duration


def escape_ffmpeg_filter_path(value):
    """
    Escape path characters for FFmpeg filter syntax.
    """
    value = str(value)
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value


def timestamp_to_seconds(value):
    """
    Parse FFmpeg progress value such as 00:01:23.456789.
    """
    try:
        hours, minutes, seconds = value.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0


def run_ffmpeg_with_progress(command, job_id, duration):
    """
    Run one continuous FFmpeg encode and update status.json.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    recent_output = deque(maxlen=40)

    if process.stdout is None:
        raise RuntimeError("Unable to read FFmpeg output")

    for raw_line in process.stdout:
        line = raw_line.strip()

        if not line:
            continue

        recent_output.append(line)

        if line.startswith("out_time="):
            encoded_seconds = timestamp_to_seconds(
                line.split("=", 1)[1]
            )

            if duration > 0:
                percent = 20 + int(
                    min(encoded_seconds / duration, 1.0) * 75
                )
                update_status(
                    job_id,
                    status="encoding",
                    progress=min(percent, 95)
                )

        elif line == "progress=end":
            update_status(
                job_id,
                status="finalizing",
                progress=97
            )

    return_code = process.wait()

    if return_code != 0:
        details = "\n".join(recent_output)
        raise RuntimeError(
            f"FFmpeg failed with code {return_code}: {details[-1500:]}"
        )


def encode_job(job_id, video_url, subtitle_path):
    job_dir = JOBS_DIR / job_id
    video_path = job_dir / "input.mp4"
    ass_path = job_dir / "subtitles.ass"
    output_path = job_dir / "output.mp4"

    try:
        update_status(
            job_id,
            status="downloading",
            progress=5
        )

        download_video(video_url, video_path)

        update_status(
            job_id,
            status="probing",
            progress=10
        )

        duration = probe_duration(video_path)

        update_status(
            job_id,
            status="preparing",
            progress=15,
            duration=duration
        )

        cues = parse_subtitles(subtitle_path)

        if not cues:
            raise ValueError(
                "No valid subtitles were found in the uploaded file"
            )

        create_ass_subtitle(cues, ass_path)

        update_status(
            job_id,
            status="encoding",
            progress=20,
            total_cues=len(cues)
        )

        ass_filter_path = escape_ffmpeg_filter_path(ass_path)
        fonts_filter_path = escape_ffmpeg_filter_path(FONTS_DIR)

        subtitle_filter = (
            f"subtitles=filename='{ass_filter_path}':"
            f"fontsdir='{fonts_filter_path}'"
        )

        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel", "error",

            "-i", str(video_path),

            "-map", "0:v:0",
            "-map", "0:a:0?",

            "-vf", subtitle_filter,

            # Video ကို တစ်ခါတည်း encode လုပ်တာကြောင့်
            # segment boundary timestamp ပြဿနာမရှိတော့ပါ။
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            "-threads", "2",

            # Browser/device compatibility အတွက် AAC သုံးသည်။
            "-c:a", "aac",
            "-b:a", "128k",

            # Input variable frame rate/timestamps ကို မလိုအပ်ဘဲ
            # fixed FPS အဖြစ် force မလုပ်ရန်။
            "-fps_mode", "passthrough",

            "-map_metadata", "0",
            "-map_chapters", "0",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "2048",
            "-movflags", "+faststart",

            "-progress", "pipe:1",
            "-nostats",

            str(output_path)
        ]

        run_ffmpeg_with_progress(
            command,
            job_id,
            duration
        )

        if (
            not output_path.exists()
            or output_path.stat().st_size < 1000
        ):
            raise RuntimeError("Output video was not created correctly")

        # Output နဲ့ status ပဲထားပြီး input/intermediate files ဖျက်မယ်။
        video_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)
        Path(subtitle_path).unlink(missing_ok=True)

        update_status(
            job_id,
            status="done",
            progress=100,
            output=output_path.name,
            size=output_path.stat().st_size
        )

    except Exception as exc:
        update_status(
            job_id,
            status="error",
            progress=0,
            error=str(exc)[:1500]
        )


def cleanup_old_jobs():
    now = datetime.now()
    removed = 0

    if not JOBS_DIR.exists():
        return removed

    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue

        status_file = job_dir / "status.json"
        status_data = read_status_file(status_file)

        try:
            if status_data and status_data.get("created"):
                created = datetime.fromisoformat(
                    status_data["created"]
                )
            else:
                created = datetime.fromtimestamp(
                    job_dir.stat().st_mtime
                )

            if now - created > timedelta(hours=JOB_TTL_HOURS):
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
        except (ValueError, OSError):
            continue

    return removed


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/submit", methods=["POST"])
def submit():
    video_url = request.form.get("video_url", "").strip()

    if not video_url:
        return jsonify({"error": "Video URL required"}), 400

    if not re.match(r"^https?://", video_url, re.IGNORECASE):
        return jsonify({
            "error": "Video URL must start with http:// or https://"
        }), 400

    if "subtitle" not in request.files:
        return jsonify({"error": "Subtitle file required"}), 400

    subtitle_file = request.files["subtitle"]

    if not subtitle_file.filename:
        return jsonify({"error": "No subtitle file selected"}), 400

    extension = Path(subtitle_file.filename).suffix.lower()

    if extension not in {".vtt", ".srt"}:
        return jsonify({
            "error": "Only VTT or SRT subtitle files are supported"
        }), 400

    cleanup_old_jobs()

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    subtitle_path = job_dir / f"subtitle{extension}"
    subtitle_file.save(str(subtitle_path))

    # Thread မစခင် status file အရင်ရေးရမယ်။
    # မူရင်း code မှာ thread စပြီးမှ queued ရေးထားတာကြောင့်
    # downloading status ကို queued ကပြန်ဖုံးနိုင်တဲ့ race ရှိတယ်။
    write_json_atomic(
        job_dir / "status.json",
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "created": now_iso(),
            "updated": now_iso()
        }
    )

    worker = threading.Thread(
        target=encode_job,
        args=(job_id, video_url, str(subtitle_path)),
        daemon=True,
        name=f"encode-{job_id}"
    )
    worker.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    if not valid_job_id(job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    status_file = JOBS_DIR / job_id / "status.json"
    status_data = read_status_file(status_file)

    if status_data is None:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(status_data)


@app.route("/api/jobs")
def jobs():
    raw_ids = request.args.get("ids", "")
    requested_ids = raw_ids.split(",")[:MAX_HISTORY_IDS]

    result = []

    for job_id in requested_ids:
        job_id = job_id.strip()

        if not valid_job_id(job_id):
            continue

        status_file = JOBS_DIR / job_id / "status.json"
        status_data = read_status_file(status_file)

        if status_data:
            status_data["job_id"] = job_id
            result.append(status_data)
        else:
            result.append({
                "job_id": job_id,
                "status": "missing",
                "progress": 0
            })

    return jsonify({"jobs": result})


@app.route("/api/download/<job_id>")
def download(job_id):
    if not valid_job_id(job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    output_path = JOBS_DIR / job_id / "output.mp4"

    if not output_path.exists():
        return jsonify({"error": "File not ready"}), 404

    return send_file(
        str(output_path),
        as_attachment=True,
        download_name=f"subtitled-{job_id}.mp4",
        mimetype="video/mp4",
        conditional=True
    )


@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    removed = cleanup_old_jobs()
    return jsonify({"removed": removed})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
