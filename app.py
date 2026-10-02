#!/usr/bin/env python3

import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
)


app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = Path(
    os.environ.get("JOBS_DIR", "/tmp/video-subtitle-jobs")
).resolve()
JOBS_DIR.mkdir(parents=True, exist_ok=True)

FONT_NAME = os.environ.get("SUBTITLE_FONT_NAME", "Padauk")
FONTS_DIR = os.environ.get("FONTS_DIR", "/usr/share/fonts")

JOB_TTL_HOURS = max(
    1,
    int(os.environ.get("JOB_TTL_HOURS", "24"))
)

DOWNLOAD_URL_EXPIRES = min(
    604800,
    max(60, int(os.environ.get("DOWNLOAD_URL_EXPIRES", "21600")))
)

MAX_SOURCE_BYTES = int(
    float(os.environ.get("MAX_SOURCE_GB", "10"))
    * 1024 * 1024 * 1024
)

MAX_CONCURRENT_JOBS = max(
    1,
    int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
)

MAX_HISTORY_IDS = 20
VALID_JOB_ID = re.compile(r"^[a-f0-9]{12}$")
REDIRECT_CODES = {301, 302, 303, 307, 308}

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY",
    ""
).strip()
R2_BUCKET = os.environ.get("R2_BUCKET", "").strip()

JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
R2_CLIENT = None
R2_CLIENT_LOCK = threading.Lock()

TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=16 * 1024 * 1024,
    multipart_chunksize=16 * 1024 * 1024,
    max_concurrency=2,
    max_io_queue=2,
    io_chunksize=1024 * 1024,
    use_threads=True,
)


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def valid_job_id(job_id):
    return bool(VALID_JOB_ID.fullmatch(job_id or ""))


def r2_is_configured():
    return all([
        R2_ACCOUNT_ID,
        R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY,
        R2_BUCKET,
    ])


def get_r2_client():
    global R2_CLIENT

    if R2_CLIENT is not None:
        return R2_CLIENT

    if not r2_is_configured():
        raise RuntimeError(
            "R2 is not configured. Add R2_ACCOUNT_ID, "
            "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET "
            "to Railway Variables."
        )

    with R2_CLIENT_LOCK:
        if R2_CLIENT is None:
            R2_CLIENT = boto3.client(
                service_name="s3",
                endpoint_url=(
                    f"https://{R2_ACCOUNT_ID}"
                    ".r2.cloudflarestorage.com"
                ),
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=Config(
                    retries={
                        "max_attempts": 8,
                        "mode": "adaptive",
                    },
                    connect_timeout=20,
                    read_timeout=180,
                    tcp_keepalive=True,
                ),
            )

    return R2_CLIENT


def read_status_file(status_file):
    try:
        return json.loads(
            status_file.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def update_status(job_id, **changes):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status_file = job_dir / "status.json"
    current = read_status_file(status_file) or {}

    current.update(changes)
    current["updated"] = now_iso()

    write_json_atomic(status_file, current)
    return current


def parse_timestamp(value):
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

        return max(
            0.0,
            hours * 3600 + minutes * 60 + seconds
        )
    except ValueError:
        return None


def clean_subtitle_line(text):
    text = re.sub(r"<[^>]*>", "", text)
    text = html.unescape(text)
    return text.strip()


def parse_subtitles(path):
    content = Path(path).read_text(
        encoding="utf-8-sig",
        errors="replace",
    )

    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", content.strip())

    cues = []

    for block in blocks:
        lines = [
            line.strip("\ufeff")
            for line in block.splitlines()
        ]

        timing_index = None

        for index, line in enumerate(lines):
            if "-->" in line:
                timing_index = index
                break

        if timing_index is None:
            continue

        timing_line = lines[timing_index]
        left, right = timing_line.split("-->", 1)

        start_token = (
            left.strip().split()[0]
            if left.strip()
            else ""
        )

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
    total_centiseconds = int(
        round(max(0.0, float(seconds)) * 100)
    )

    hours, remainder = divmod(
        total_centiseconds,
        360000,
    )
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)

    return (
        f"{hours}:{minutes:02d}:"
        f"{secs:02d}.{centiseconds:02d}"
    )


def escape_ass_text(text):
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    return text


def create_ass_subtitle(cues, output_path):
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
Style: Default,{FONT_NAME},72,&H00FFFFFF,&H00FFFFFF,&H60000000,&H60000000,-1,0,0,0,100,100,0,0,3,4,0,2,60,60,48,1

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

        subtitle_text = r"\N".join(cleaned_lines)

        events.append(
            "Dialogue: 0,"
            f"{ass_time(start)},"
            f"{ass_time(end)},"
            "Default,,0,0,0,,"
            f"{subtitle_text}"
        )

    output_path.write_text(
        header + "\n".join(events) + "\n",
        encoding="utf-8",
    )


def validate_public_host(url):
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "Video URL must use http:// or https://"
        )

    if not parsed.hostname:
        raise ValueError("Video URL hostname is missing")

    if parsed.username or parsed.password:
        raise ValueError(
            "Username/password in Video URL is not allowed"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid Video URL port") from exc

    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(
            "Video URL hostname cannot be resolved"
        ) from exc

    if not addresses:
        raise ValueError(
            "Video URL hostname cannot be resolved"
        )

    for address in addresses:
        ip_text = address[4][0]
        ip = ipaddress.ip_address(ip_text)

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                "Private/internal network URLs are not allowed"
            )


def validate_remote_video_url(url):
    current_url = url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }

    session = requests.Session()

    try:
        for _ in range(6):
            validate_public_host(current_url)

            response = session.get(
                current_url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(20, 60),
            )

            try:
                if response.status_code in REDIRECT_CODES:
                    location = response.headers.get("Location")

                    if not location:
                        raise ValueError(
                            "Video URL redirect has no Location"
                        )

                    current_url = urljoin(
                        current_url,
                        location,
                    )
                    continue

                response.raise_for_status()

                total_size = None
                content_range = response.headers.get(
                    "Content-Range",
                    "",
                )

                range_match = re.search(
                    r"/(\d+)$",
                    content_range,
                )

                if range_match:
                    total_size = int(range_match.group(1))
                elif response.status_code == 200:
                    content_length = response.headers.get(
                        "Content-Length"
                    )

                    if content_length and content_length.isdigit():
                        total_size = int(content_length)

                if (
                    total_size is not None
                    and total_size > MAX_SOURCE_BYTES
                ):
                    max_gb = MAX_SOURCE_BYTES / 1024 ** 3

                    raise ValueError(
                        f"Video is too large. Maximum is "
                        f"{max_gb:g} GB."
                    )

                return current_url, total_size

            finally:
                response.close()

        raise ValueError("Too many Video URL redirects")

    finally:
        session.close()


def probe_duration(video_url):
    command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "180000000",
        "-user_agent",
        (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Timed out while reading video information"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "Cannot read video duration: "
            + (
                result.stderr.strip()[-1000:]
                or "ffprobe failed"
            )
        )

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            "Invalid video duration"
        ) from exc

    if duration <= 0:
        raise RuntimeError(
            "Video duration is zero or invalid"
        )

    return duration


def escape_ffmpeg_filter_path(value):
    value = str(value)
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value


def timestamp_to_seconds(value):
    try:
        hours, minutes, seconds = value.strip().split(":")

        return (
            int(hours) * 3600
            + int(minutes) * 60
            + float(seconds)
        )
    except (ValueError, AttributeError):
        return 0.0


def delete_r2_object(object_key):
    if not object_key or not r2_is_configured():
        return

    get_r2_client().delete_object(
        Bucket=R2_BUCKET,
        Key=object_key,
    )


def stream_ffmpeg_to_r2(
    command,
    job_id,
    duration,
    object_key,
):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError(
            "Unable to open FFmpeg output streams"
        )

    recent_output = deque(maxlen=80)

    def read_ffmpeg_status():
        for raw_line in iter(process.stderr.readline, b""):
            line = raw_line.decode(
                "utf-8",
                errors="replace",
            ).strip()

            if not line:
                continue

            if line.startswith("out_time="):
                encoded_seconds = timestamp_to_seconds(
                    line.split("=", 1)[1]
                )

                if duration > 0:
                    progress = 15 + int(
                        min(
                            encoded_seconds / duration,
                            1.0,
                        ) * 80
                    )

                    update_status(
                        job_id,
                        status="encoding",
                        progress=min(progress, 95),
                    )

            elif line == "progress=end":
                update_status(
                    job_id,
                    status="uploading",
                    progress=97,
                )

            elif not re.match(
                r"^[a-zA-Z0-9_]+=",
                line,
            ):
                recent_output.append(line)

    stderr_thread = threading.Thread(
        target=read_ffmpeg_status,
        daemon=True,
        name=f"ffmpeg-status-{job_id}",
    )
    stderr_thread.start()

    client = get_r2_client()

    try:
        client.upload_fileobj(
            process.stdout,
            R2_BUCKET,
            object_key,
            ExtraArgs={
                "ContentType": "video/mp4",
                "ContentDisposition": (
                    f'attachment; filename="'
                    f'subtitled-{job_id}.mp4"'
                ),
            },
            Config=TRANSFER_CONFIG,
        )

        process.stdout.close()
        return_code = process.wait()
        stderr_thread.join(timeout=10)

        if return_code != 0:
            delete_r2_object(object_key)

            details = "\n".join(recent_output)

            raise RuntimeError(
                f"FFmpeg failed with code {return_code}: "
                f"{details[-1800:]}"
            )

        metadata = client.head_object(
            Bucket=R2_BUCKET,
            Key=object_key,
        )

        size = int(metadata.get("ContentLength", 0))

        if size < 1000:
            delete_r2_object(object_key)

            raise RuntimeError(
                "Output video was not created correctly"
            )

        return size

    except Exception:
        if process.poll() is None:
            process.kill()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

        try:
            delete_r2_object(object_key)
        except Exception:
            pass

        raise

    finally:
        try:
            process.stdout.close()
        except Exception:
            pass

        try:
            process.stderr.close()
        except Exception:
            pass


def encode_job(job_id, video_url, subtitle_path):
    job_dir = JOBS_DIR / job_id
    ass_path = job_dir / "subtitles.ass"
    object_key = f"outputs/{job_id}.mp4"

    try:
        update_status(
            job_id,
            status="waiting",
            progress=1,
        )

        with JOB_SEMAPHORE:
            update_status(
                job_id,
                status="checking",
                progress=3,
            )

            safe_video_url, source_size = (
                validate_remote_video_url(video_url)
            )

            update_status(
                job_id,
                status="probing",
                progress=7,
                source_size=source_size,
            )

            duration = probe_duration(safe_video_url)

            update_status(
                job_id,
                status="preparing",
                progress=10,
                duration=duration,
            )

            cues = parse_subtitles(subtitle_path)

            if not cues:
                raise ValueError(
                    "No valid subtitles were found "
                    "in the uploaded file"
                )

            create_ass_subtitle(cues, ass_path)

            ass_filter_path = escape_ffmpeg_filter_path(
                ass_path
            )
            fonts_filter_path = escape_ffmpeg_filter_path(
                FONTS_DIR
            )

            subtitle_filter = (
                f"subtitles=filename='{ass_filter_path}':"
                f"fontsdir='{fonts_filter_path}'"
            )

            update_status(
                job_id,
                status="encoding",
                progress=15,
                total_cues=len(cues),
            )

            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",

                "-rw_timeout", "180000000",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-user_agent",
                (
                    "Mozilla/5.0 (Linux; Android 13) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                ),

                "-i", safe_video_url,

                "-map", "0:v:0",
                "-map", "0:a:0?",

                "-vf", subtitle_filter,

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-threads", "2",

                "-c:a", "aac",
                "-b:a", "128k",

                "-fps_mode", "passthrough",
                "-map_metadata", "-1",
                "-map_chapters", "-1",
                "-avoid_negative_ts", "make_zero",
                "-max_muxing_queue_size", "2048",

                "-movflags",
                (
                    "+frag_keyframe"
                    "+empty_moov"
                    "+default_base_moof"
                ),
                "-frag_duration", "2000000",

                "-progress", "pipe:2",
                "-nostats",

                "-f", "mp4",
                "pipe:1",
            ]

            output_size = stream_ffmpeg_to_r2(
                command,
                job_id,
                duration,
                object_key,
            )

            update_status(
                job_id,
                status="done",
                progress=100,
                output_key=object_key,
                size=output_size,
            )

    except Exception as exc:
        try:
            delete_r2_object(object_key)
        except Exception:
            pass

        update_status(
            job_id,
            status="error",
            progress=0,
            error=str(exc)[:1800],
        )

    finally:
        Path(subtitle_path).unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)


def parse_created_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def cleanup_old_jobs():
    cutoff = now_utc() - timedelta(hours=JOB_TTL_HOURS)
    removed = 0

    if JOBS_DIR.exists():
        for job_dir in JOBS_DIR.iterdir():
            if not job_dir.is_dir():
                continue

            status_data = read_status_file(
                job_dir / "status.json"
            ) or {}

            created = parse_created_time(
                status_data.get("created")
            )

            if created is None:
                try:
                    created = datetime.fromtimestamp(
                        job_dir.stat().st_mtime,
                        tz=timezone.utc,
                    )
                except OSError:
                    continue

            if created >= cutoff:
                continue

            object_key = status_data.get("output_key")

            if object_key:
                try:
                    delete_r2_object(object_key)
                except Exception:
                    continue

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )
            removed += 1

    if r2_is_configured():
        client = get_r2_client()
        continuation_token = None

        while True:
            arguments = {
                "Bucket": R2_BUCKET,
                "Prefix": "outputs/",
                "MaxKeys": 1000,
            }

            if continuation_token:
                arguments["ContinuationToken"] = (
                    continuation_token
                )

            response = client.list_objects_v2(**arguments)

            for item in response.get("Contents", []):
                modified = item.get("LastModified")

                if modified and modified < cutoff:
                    client.delete_object(
                        Bucket=R2_BUCKET,
                        Key=item["Key"],
                    )
                    removed += 1

            if not response.get("IsTruncated"):
                break

            continuation_token = response.get(
                "NextContinuationToken"
            )

            if not continuation_token:
                break

    return removed


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/submit", methods=["POST"])
def submit():
    if not r2_is_configured():
        return jsonify({
            "error": (
                "R2 storage is not configured. "
                "Add the R2 variables in Railway."
            )
        }), 503

    video_url = request.form.get(
        "video_url",
        "",
    ).strip()

    if not video_url:
        return jsonify({
            "error": "Video URL required"
        }), 400

    if not re.match(
        r"^https?://",
        video_url,
        re.IGNORECASE,
    ):
        return jsonify({
            "error": (
                "Video URL must start with "
                "http:// or https://"
            )
        }), 400

    if "subtitle" not in request.files:
        return jsonify({
            "error": "Subtitle file required"
        }), 400

    subtitle_file = request.files["subtitle"]

    if not subtitle_file.filename:
        return jsonify({
            "error": "No subtitle file selected"
        }), 400

    extension = Path(
        subtitle_file.filename
    ).suffix.lower()

    if extension not in {".vtt", ".srt"}:
        return jsonify({
            "error": (
                "Only VTT or SRT subtitle "
                "files are supported"
            )
        }), 400

    try:
        cleanup_old_jobs()
    except Exception:
        pass

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    subtitle_path = job_dir / f"subtitle{extension}"
    subtitle_file.save(str(subtitle_path))

    write_json_atomic(
        job_dir / "status.json",
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "created": now_iso(),
            "updated": now_iso(),
        },
    )

    worker = threading.Thread(
        target=encode_job,
        args=(
            job_id,
            video_url,
            str(subtitle_path),
        ),
        daemon=True,
        name=f"encode-{job_id}",
    )
    worker.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    if not valid_job_id(job_id):
        return jsonify({
            "error": "Invalid job ID"
        }), 400

    status_data = read_status_file(
        JOBS_DIR / job_id / "status.json"
    )

    if status_data is None:
        return jsonify({
            "error": "Job not found"
        }), 404

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

        status_data = read_status_file(
            JOBS_DIR / job_id / "status.json"
        )

        if status_data:
            status_data["job_id"] = job_id
            result.append(status_data)
        else:
            result.append({
                "job_id": job_id,
                "status": "missing",
                "progress": 0,
            })

    return jsonify({"jobs": result})


@app.route("/api/download/<job_id>")
def download(job_id):
    if not valid_job_id(job_id):
        return jsonify({
            "error": "Invalid job ID"
        }), 400

    status_data = read_status_file(
        JOBS_DIR / job_id / "status.json"
    )

    if not status_data:
        return jsonify({
            "error": "Job not found"
        }), 404

    if status_data.get("status") != "done":
        return jsonify({
            "error": "File is not ready"
        }), 409

    object_key = status_data.get("output_key")

    if not object_key:
        return jsonify({
            "error": "Output object is missing"
        }), 404

    try:
        download_url = get_r2_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": object_key,
                "ResponseContentDisposition": (
                    f'attachment; filename="'
                    f'subtitled-{job_id}.mp4"'
                ),
                "ResponseContentType": "video/mp4",
            },
            ExpiresIn=DOWNLOAD_URL_EXPIRES,
        )
    except Exception as exc:
        return jsonify({
            "error": (
                "Unable to create download link: "
                + str(exc)[:500]
            )
        }), 500

    return redirect(download_url, code=302)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    if not valid_job_id(job_id):
        return jsonify({
            "error": "Invalid job ID"
        }), 400

    job_dir = JOBS_DIR / job_id
    status_data = read_status_file(
        job_dir / "status.json"
    )

    if not status_data:
        return jsonify({
            "deleted": True,
            "job_id": job_id,
        })

    current_status = status_data.get("status")

    if current_status not in {
        "done",
        "error",
        "missing",
    }:
        return jsonify({
            "error": (
                "Encoding is still running. "
                "Wait until it finishes before deleting."
            )
        }), 409

    object_key = status_data.get("output_key")

    try:
        if object_key:
            delete_r2_object(object_key)

        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        return jsonify({
            "deleted": True,
            "job_id": job_id,
        })

    except Exception as exc:
        return jsonify({
            "error": (
                "Delete failed: "
                + str(exc)[:500]
            )
        }), 500


@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    try:
        removed = cleanup_old_jobs()

        return jsonify({
            "removed": removed
        })
    except Exception as exc:
        return jsonify({
            "error": str(exc)[:1000]
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
