#!/usr/bin/env python3

import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
)


app = Flask(__name__)

MAX_SUBTITLE_MB = max(
    1,
    int(os.environ.get("MAX_SUBTITLE_MB", "10"))
)

app.config["MAX_CONTENT_LENGTH"] = (
    MAX_SUBTITLE_MB * 1024 * 1024
)

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

BUNNY_STORAGE_ZONE = os.environ.get(
    "BUNNY_STORAGE_ZONE",
    "",
).strip()

BUNNY_STORAGE_PASSWORD = os.environ.get(
    "BUNNY_STORAGE_PASSWORD",
    "",
).strip()

BUNNY_STORAGE_HOSTNAME = os.environ.get(
    "BUNNY_STORAGE_HOSTNAME",
    "storage.bunnycdn.com",
).strip()

BUNNY_CDN_HOSTNAME = os.environ.get(
    "BUNNY_CDN_HOSTNAME",
    "",
).strip()

BUNNY_TOKEN_AUTH_KEY = os.environ.get(
    "BUNNY_TOKEN_AUTH_KEY",
    "",
).strip()

BUNNY_UPLOAD_RETRIES = max(
    1,
    int(os.environ.get("BUNNY_UPLOAD_RETRIES", "4")),
)

BUNNY_CONNECT_TIMEOUT = max(
    5,
    int(os.environ.get("BUNNY_CONNECT_TIMEOUT", "30")),
)

BUNNY_READ_TIMEOUT = max(
    60,
    int(os.environ.get("BUNNY_READ_TIMEOUT", "900")),
)

BUNNY_UPLOAD_CHUNK_MB = max(
    1,
    int(os.environ.get("BUNNY_UPLOAD_CHUNK_MB", "1")),
)

BUNNY_UPLOAD_CHUNK_SIZE = (
    BUNNY_UPLOAD_CHUNK_MB * 1024 * 1024
)

BUNNY_RETRYABLE_STATUS_CODES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
}

JOB_SEMAPHORE = threading.BoundedSemaphore(
    MAX_CONCURRENT_JOBS
)


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def valid_job_id(job_id):
    return bool(VALID_JOB_ID.fullmatch(job_id or ""))


def normalize_hostname(value):
    value = (value or "").strip()

    value = re.sub(
        r"^https?://",
        "",
        value,
        flags=re.IGNORECASE,
    )

    return value.strip("/")


BUNNY_STORAGE_HOSTNAME = normalize_hostname(
    BUNNY_STORAGE_HOSTNAME
)

BUNNY_CDN_HOSTNAME = normalize_hostname(
    BUNNY_CDN_HOSTNAME
)


def bunny_is_configured():
    return all([
        BUNNY_STORAGE_ZONE,
        BUNNY_STORAGE_PASSWORD,
        BUNNY_STORAGE_HOSTNAME,
        BUNNY_CDN_HOSTNAME,
    ])


def bunny_storage_headers():
    return {
        "AccessKey": BUNNY_STORAGE_PASSWORD,
        "Accept": "application/json",
    }


def bunny_storage_url(object_key=""):
    zone = quote(
        BUNNY_STORAGE_ZONE,
        safe="",
    )

    clean_key = str(object_key or "").lstrip("/")
    encoded_key = quote(clean_key, safe="/")

    base_url = (
        f"https://{BUNNY_STORAGE_HOSTNAME}/{zone}"
    )

    if not encoded_key:
        return base_url + "/"

    return f"{base_url}/{encoded_key}"


def bunny_cdn_url(object_key):
    encoded_key = quote(
        str(object_key).lstrip("/"),
        safe="/",
    )

    path = f"/{encoded_key}"
    base_url = f"https://{BUNNY_CDN_HOSTNAME}{path}"

    if not BUNNY_TOKEN_AUTH_KEY:
        return base_url

    expires = int(time.time()) + DOWNLOAD_URL_EXPIRES

    token_input = (
        f"{BUNNY_TOKEN_AUTH_KEY}"
        f"{path}"
        f"{expires}"
    )

    digest = hashlib.md5(
        token_input.encode("utf-8")
    ).digest()

    token = base64.urlsafe_b64encode(
        digest
    ).decode("ascii").rstrip("=")

    return (
        f"{base_url}"
        f"?token={quote(token, safe='')}"
        f"&expires={expires}"
    )


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


# --- PNG subtitle rendering (PIL + RAQM for correct Myanmar shaping) ---
# Replaces libass/ASS which cannot shape Myanmar medial signs correctly.

PADAUK_BOLD = "/usr/share/fonts/truetype/padauk/Padauk-Bold.ttf"
PADAUK_REGULAR = "/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf"
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

SUBTITLE_FONT_SIZE = 32  # Compact subtitle size for 720p


def _is_myanmar(char):
    return "\u1000" <= char <= "\u109F"


def _get_font_for_text(text, size):
    """Pick font based on script: Padauk for Myanmar, DejaVu for Latin."""
    has_mm = any(_is_myanmar(c) for c in text)
    if has_mm:
        path = PADAUK_BOLD if Path(PADAUK_BOLD).exists() else DEJAVU_BOLD
    else:
        path = DEJAVU_BOLD
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text_to_lines(text, font, max_width, draw):
    """Wrap text into multiple lines that fit max_width. Wraps on spaces."""
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines if lines else [text]


def render_subtitle_png(text_lines, output_path, video_width=1280):
    """Render subtitle cue as transparent PNG.

    Style: yellow text with thick black outline/shadow, no background box.
    Long lines are wrapped to fit within video width.
    """
    font = _get_font_for_text(" ".join(text_lines), SUBTITLE_FONT_SIZE)

    tmp_img = Image.new("RGBA", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp_img)

    # Wrap long lines to fit video width
    max_text_w = video_width - 120
    wrapped = []
    for line in text_lines:
        bbox = tmp_draw.textbbox((0, 0), line, font=font)
        if bbox[2] - bbox[0] > max_text_w:
            wrapped.extend(_wrap_text_to_lines(line, font, max_text_w, tmp_draw))
        else:
            wrapped.append(line)
    # Limit to 3 lines max
    wrapped = wrapped[:3]

    # Measure (include stroke width + extra for Myanmar descenders)
    STROKE = 3
    line_heights = []
    line_widths = []
    line_tops = []  # bbox top offset for each line
    for line in wrapped:
        bbox = tmp_draw.textbbox(
            (0, 0), line, font=font,
            stroke_width=STROKE,
        )
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
        line_tops.append(bbox[1])

    max_w = max(line_widths) if line_widths else 10
    # Extra 10px per line for deep Myanmar descenders
    total_h = sum(h + 10 for h in line_heights) + (len(wrapped) - 1) * 6

    pad = 10
    img_w = max_w + pad * 2
    img_h = total_h + pad * 2

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Yellow text with thick black outline (no background box)
    y = pad
    for i, line in enumerate(wrapped):
        lw = line_widths[i]
        x = (img_w - lw) // 2
        # Offset by -top so ascenders/descenders aren't clipped
        draw.text(
            (x, y - line_tops[i]), line, font=font,
            fill=(255, 235, 59, 255),  # Yellow
            stroke_width=STROKE, stroke_fill=(0, 0, 0, 255),
        )
        y += line_heights[i] + 10 + 6

    img.save(output_path)
    return output_path


def render_all_subtitles(cues, png_dir, video_width=1280):
    """Render all cues to PNGs. Returns [(png_path, start, end), ...]."""
    png_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for idx, (start, end, text_lines) in enumerate(cues):
        png_path = png_dir / f"cue_{idx:04d}.png"
        render_subtitle_png(text_lines, str(png_path), video_width)
        result.append((str(png_path), start, end))
    return result



WATERMARK_TEXT = "lugyiapplication.vercel.app"
INTRO_LINK = "lugyiapplication.vercel.app"
INTRO_SUFFIX = "မှ တင်ဆက်သည်"


def render_watermark_png(output_path):
    """Render top-right watermark: white text with heavy black shadow."""
    font = _get_font_for_text(WATERMARK_TEXT, 28)
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), WATERMARK_TEXT, font=font, stroke_width=3)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 12
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Heavy black shadow for visibility on white scenes
    draw.text(
        (pad - bbox[0], pad - bbox[1]), WATERMARK_TEXT, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=3, stroke_fill=(0, 0, 0, 255),
    )
    # Extra dark glow behind
    img.save(output_path)
    return output_path


def render_intro_png(output_path, video_width=1280):
    """Render intro: link in cyan + suffix in white, baseline-aligned."""
    font_link = _get_font_for_text(INTRO_LINK, 44)
    font_suffix = _get_font_for_text(INTRO_SUFFIX, 44)
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    # Measure with baseline anchor for proper alignment
    b1 = d.textbbox((0, 0), INTRO_LINK, font=font_link,
                     stroke_width=3, anchor="ls")
    b2 = d.textbbox((0, 0), INTRO_SUFFIX, font=font_suffix,
                     stroke_width=3, anchor="ls")
    w1 = b1[2] - b1[0]
    w2 = b2[2] - b2[0]
    # Height: from top of tallest ascender to bottom of deepest descender
    top = min(b1[1], b2[1])
    bottom = max(b1[3], b2[3])
    h = bottom - top
    gap = 16
    total_w = w1 + gap + w2
    pad = 20
    img = Image.new(
        "RGBA", (total_w + pad * 2, h + pad * 2), (0, 0, 0, 0)
    )
    draw = ImageDraw.Draw(img)
    # Baseline y (same for both = aligned)
    baseline_y = pad - top
    x = pad - b1[0]
    # Link in cyan (anchor ls = left-baseline)
    draw.text(
        (x, baseline_y), INTRO_LINK, font=font_link,
        fill=(0, 229, 255, 255), anchor="ls",
        stroke_width=3, stroke_fill=(0, 0, 0, 255),
    )
    x += w1 + gap - b2[0] + b1[0]
    # Suffix in white (same baseline)
    draw.text(
        (x, baseline_y), INTRO_SUFFIX, font=font_suffix,
        fill=(255, 255, 255, 255), anchor="ls",
        stroke_width=3, stroke_fill=(0, 0, 0, 255),
    )
    img.save(output_path)
    return output_path


def run_ffmpeg_simple(command, label):
    """Run ffmpeg without progress tracking (for segments)."""
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        err = result.stderr[-1500:] if result.stderr else "unknown"
        raise RuntimeError(f"{label} failed: {err}")
    return True



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


def delete_bunny_object(object_key):
    if not object_key or not bunny_is_configured():
        return

    response = None

    try:
        response = requests.delete(
            bunny_storage_url(object_key),
            headers=bunny_storage_headers(),
            timeout=(
                BUNNY_CONNECT_TIMEOUT,
                120,
            ),
        )

        if response.status_code not in {
            200,
            204,
            404,
        }:
            details = response.text[:500]

            raise RuntimeError(
                "Bunny delete failed: "
                f"HTTP {response.status_code} "
                f"{details}"
            )

    finally:
        if response is not None:
            response.close()


def calculate_sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as file_handle:
        while True:
            chunk = file_handle.read(
                4 * 1024 * 1024
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest().upper()


class UploadProgressReader:
    def __init__(
        self,
        file_handle,
        total_size,
        callback,
    ):
        self.file_handle = file_handle
        self.total_size = total_size
        self.callback = callback
        self.bytes_read = 0
        self.last_update = 0.0

    def __len__(self):
        return self.total_size

    def tell(self):
        return self.bytes_read

    def read(self, size=-1):
        if size is None or size < 0:
            size = BUNNY_UPLOAD_CHUNK_SIZE
        else:
            size = min(
                size,
                BUNNY_UPLOAD_CHUNK_SIZE,
            )

        data = self.file_handle.read(size)

        if data:
            self.bytes_read += len(data)

            current_time = time.monotonic()

            if (
                current_time - self.last_update >= 1.0
                or self.bytes_read >= self.total_size
            ):
                self.callback(
                    self.bytes_read,
                    self.total_size,
                )
                self.last_update = current_time

        return data


def parse_retry_after(response):
    if response is None:
        return None

    value = response.headers.get(
        "Retry-After",
        "",
    ).strip()

    try:
        return max(1, min(60, int(value)))
    except (TypeError, ValueError):
        return None


def verify_bunny_upload(
    object_key,
    expected_size,
):
    last_error = None

    for attempt in range(1, 4):
        response = None

        try:
            headers = bunny_storage_headers()
            headers["Range"] = "bytes=0-0"

            response = requests.get(
                bunny_storage_url(object_key),
                headers=headers,
                stream=True,
                timeout=(
                    BUNNY_CONNECT_TIMEOUT,
                    120,
                ),
            )

            if response.status_code not in {
                200,
                206,
            }:
                raise RuntimeError(
                    "Bunny verification failed: "
                    f"HTTP {response.status_code}"
                )

            remote_size = None

            content_range = response.headers.get(
                "Content-Range",
                "",
            )

            range_match = re.search(
                r"/(\d+)$",
                content_range,
            )

            if range_match:
                remote_size = int(
                    range_match.group(1)
                )
            else:
                content_length = (
                    response.headers.get(
                        "Content-Length",
                        "",
                    )
                )

                if content_length.isdigit():
                    remote_size = int(content_length)

            if remote_size is None:
                raise RuntimeError(
                    "Bunny did not return the uploaded "
                    "file size"
                )

            if remote_size != expected_size:
                raise RuntimeError(
                    "Bunny upload size mismatch: "
                    f"local={expected_size}, "
                    f"remote={remote_size}"
                )

            return

        except Exception as exc:
            last_error = exc

            if attempt < 3:
                time.sleep(attempt * 2)

        finally:
            if response is not None:
                response.close()

    raise RuntimeError(
        "Unable to verify Bunny upload: "
        f"{last_error}"
    )


def upload_file_to_bunny(
    file_path,
    object_key,
    job_id,
):
    file_path = Path(file_path)
    file_size = file_path.stat().st_size

    if file_size < 1000:
        raise RuntimeError(
            "Output video was not created correctly"
        )

    update_status(
        job_id,
        status="uploading",
        progress=91,
        upload_bytes=0,
        upload_total=file_size,
    )

    checksum = calculate_sha256(file_path)
    last_error = None

    for attempt in range(
        1,
        BUNNY_UPLOAD_RETRIES + 1,
    ):
        response = None

        update_status(
            job_id,
            status="uploading",
            progress=92,
            upload_attempt=attempt,
            upload_retries=BUNNY_UPLOAD_RETRIES,
            upload_bytes=0,
            upload_total=file_size,
        )

        def progress_callback(sent, total):
            ratio = (
                sent / total
                if total > 0
                else 0
            )

            progress = 92 + int(
                min(max(ratio, 0.0), 1.0) * 7
            )

            update_status(
                job_id,
                status="uploading",
                progress=min(progress, 99),
                upload_bytes=sent,
                upload_total=total,
                upload_attempt=attempt,
            )

        try:
            with file_path.open("rb") as file_handle:
                reader = UploadProgressReader(
                    file_handle,
                    file_size,
                    progress_callback,
                )

                headers = bunny_storage_headers()
                headers.update({
                    "Content-Type": "video/mp4",
                    "Content-Length": str(file_size),
                    "Checksum": checksum,
                })

                response = requests.put(
                    bunny_storage_url(object_key),
                    headers=headers,
                    data=reader,
                    timeout=(
                        BUNNY_CONNECT_TIMEOUT,
                        BUNNY_READ_TIMEOUT,
                    ),
                )

            if response.status_code != 201:
                details = response.text[:700]

                raise RuntimeError(
                    "Bunny upload failed: "
                    f"HTTP {response.status_code} "
                    f"{details}"
                )

            update_status(
                job_id,
                status="verifying",
                progress=99,
                upload_bytes=file_size,
                upload_total=file_size,
            )

            verify_bunny_upload(
                object_key,
                file_size,
            )

            return file_size

        except Exception as exc:
            last_error = exc

            retryable = True

            if response is not None:
                status_code = response.status_code

                if (
                    status_code >= 400
                    and status_code
                    not in BUNNY_RETRYABLE_STATUS_CODES
                ):
                    retryable = False

            if (
                not retryable
                or attempt >= BUNNY_UPLOAD_RETRIES
            ):
                break

            retry_after = parse_retry_after(response)

            delay = retry_after or min(
                30,
                2 ** (attempt - 1),
            )

            update_status(
                job_id,
                status="uploading",
                progress=92,
                upload_attempt=attempt,
                upload_retry_in=delay,
                upload_error=str(exc)[:500],
            )

            time.sleep(delay)

        finally:
            if response is not None:
                response.close()

    try:
        delete_bunny_object(object_key)
    except Exception:
        pass

    raise RuntimeError(
        "Bunny upload failed after "
        f"{BUNNY_UPLOAD_RETRIES} attempts: "
        f"{last_error}"
    )


def run_ffmpeg_to_file(
    command,
    output_path,
    job_id,
    duration,
):
    recent_output = deque(maxlen=80)

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    if process.stderr is None:
        process.kill()

        raise RuntimeError(
            "Unable to read FFmpeg output"
        )

    try:
        for raw_line in process.stderr:
            line = raw_line.strip()

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
                        ) * 75
                    )

                    update_status(
                        job_id,
                        status="encoding",
                        progress=min(progress, 90),
                    )

            elif line == "progress=end":
                update_status(
                    job_id,
                    status="encoding",
                    progress=90,
                )

            elif not re.match(
                r"^[a-zA-Z0-9_]+=",
                line,
            ):
                recent_output.append(line)

        return_code = process.wait()

        if return_code != 0:
            details = "\n".join(recent_output)

            raise RuntimeError(
                f"FFmpeg failed with code "
                f"{return_code}: "
                f"{details[-1800:]}"
            )

        output_path = Path(output_path)

        if not output_path.exists():
            raise RuntimeError(
                "FFmpeg output file is missing"
            )

        output_size = output_path.stat().st_size

        if output_size < 1000:
            raise RuntimeError(
                "Output video was not created correctly"
            )

        return output_size

    except Exception:
        if process.poll() is None:
            process.kill()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

        raise

    finally:
        try:
            process.stderr.close()
        except Exception:
            pass



def _download_video_to_file(url, dest_path, job_id):
    """Download remote video to local file with progress."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
    }
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if total > 0:
                        pct = 13 + int(2 * done / total)
                        if pct % 2 == 0:
                            update_status(
                                job_id, status="downloading",
                                progress=min(pct, 15),
                            )


def _probe_video_width(video_path):
    """Get video width via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return int(result.stdout.strip())
    except Exception:
        return None


def encode_job(job_id, video_url, subtitle_path, watermark_enabled=True):
    job_dir = JOBS_DIR / job_id
    ass_path = job_dir / "subtitles.ass"
    output_path = job_dir / "output.mp4"
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

            duration = probe_duration(
                safe_video_url
            )

            update_status(
                job_id,
                status="preparing",
                progress=10,
                duration=duration,
            )

            cues = []
            if subtitle_path and Path(subtitle_path).exists():
                cues = parse_subtitles(
                    subtitle_path
                )
            # Subtitles are optional - watermark+intro always applied

            # --- PNG subtitle rendering (correct Myanmar shaping) ---
            update_status(
                job_id,
                status="rendering",
                progress=12,
                total_cues=len(cues),
            )

            # Download video to disk for segment processing
            input_path = job_dir / "input.mp4"
            update_status(job_id, status="downloading", progress=13)
            _download_video_to_file(safe_video_url, input_path, job_id)

            # Get video dimensions for PNG sizing
            video_width = _probe_video_width(input_path) or 1280

            # Render all subtitle PNGs (if any)
            png_dir = job_dir / "pngs"
            png_files = (
                render_all_subtitles(cues, png_dir, video_width)
                if cues else []
            )

            # Render watermark and intro PNGs
            watermark_png = str(job_dir / "watermark.png")
            if watermark_enabled:
                render_watermark_png(watermark_png)
            intro_png = str(job_dir / "intro.png")
            render_intro_png(intro_png, video_width)

            update_status(
                job_id,
                status="encoding",
                progress=15,
                total_cues=len(cues),
            )

            # --- Segmented PNG overlay (all re-encoded, no stream copy) ---
            # Re-encoding everything avoids timestamp discontinuities
            # that caused freezing with mixed copy/encode segments.
            update_status(
                job_id,
                status="encoding",
                progress=15,
                total_cues=len(cues),
            )

            # Group cues: max 15 per group (safe ffmpeg input count)
            MAX_GROUP = 15
            groups = []
            cur_group = []
            for p, s, e in (png_files or []):
                if cur_group and len(cur_group) >= MAX_GROUP:
                    groups.append(cur_group)
                    cur_group = []
                cur_group.append((p, s, e))
            if cur_group:
                groups.append(cur_group)

            # Build timeline pieces
            seg_dir = job_dir / "segments"
            seg_dir.mkdir(parents=True, exist_ok=True)
            pieces = []
            cursor = 0.0
            for g in groups:
                gs = g[0][1]
                ge = g[-1][2]
                if gs > cursor + 0.05:
                    pieces.append((cursor, gs, None))
                pieces.append((gs, ge, g))
                cursor = ge
            if cursor < duration - 0.05:
                pieces.append((cursor, duration, None))

            # Encode each piece (ALL re-encoded with identical settings)
            seg_files = []
            total_pieces = len(pieces)
            for idx, (ps, pe, plist) in enumerate(pieces):
                pdur = pe - ps
                if pdur <= 0:
                    continue
                seg_out = seg_dir / f"seg_{idx:04d}.mp4"
                seg_files.append(str(seg_out))

                prog = 15 + int(70 * idx / max(total_pieces, 1))
                update_status(
                    job_id, status="encoding",
                    progress=min(prog, 85),
                )

                if plist is None:
                    # Gap: re-encode with watermark (optional) + intro (if 0-10s)
                    gap_filter = []
                    gap_inputs = []
                    prev = "[0:v]"
                    next_idx = 1

                    if watermark_enabled:
                        gap_inputs += ["-i", watermark_png]
                        gap_filter.append(
                            f"{prev}[{next_idx}:v]overlay="
                            f"x=W-w-20:y=20[wm]"
                        )
                        prev = "[wm]"
                        next_idx += 1

                    gap_inputs += ["-i", intro_png]
                    intro_s = max(0 - ps, 0)
                    intro_e = min(10 - ps, pdur)
                    if intro_e > intro_s + 0.1:
                        gap_filter.append(
                            f"{prev}[{next_idx}:v]overlay="
                            f"x=(W-w)/2:y=H-h-60:"
                            f"enable='between(t,{intro_s:.3f},"
                            f"{intro_e:.3f})'"
                            f"[vout]"
                        )
                        vmap = "[vout]"
                    elif prev != "[0:v]":
                        vmap = prev
                    else:
                        # No watermark, no intro: plain re-encode
                        gap_filter = None
                        vmap = "0:v"

                    gap_cmd = [
                        "ffmpeg", "-y", "-hide_banner",
                        "-loglevel", "error",
                        "-ss", f"{ps:.3f}",
                        "-i", str(input_path),
                        *gap_inputs,
                        "-t", f"{pdur:.3f}",
                    ]
                    if gap_filter:
                        gap_cmd += [
                            "-filter_complex", ";".join(gap_filter),
                        ]
                    gap_cmd += [
                        "-map", vmap, "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k",
                        "-avoid_negative_ts", "make_zero",
                        str(seg_out),
                    ]
                    run_ffmpeg_simple(gap_cmd, f"gap {idx}")
                else:
                    # Subtitle segment: extract then burn PNGs
                    raw_seg = seg_dir / f"raw_{idx:04d}.mp4"
                    run_ffmpeg_simple([
                        "ffmpeg", "-y", "-hide_banner",
                        "-loglevel", "error",
                        "-ss", f"{ps:.3f}",
                        "-i", str(input_path),
                        "-t", f"{pdur:.3f}",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k",
                        "-avoid_negative_ts", "make_zero",
                        str(raw_seg),
                    ], f"extract {idx}")

                    # Overlay PNGs (max 15 inputs - safe)
                    # + watermark (top-right, always) + intro (0-10s)
                    filter_parts = []
                    prev = "[0:v]"
                    png_inputs = []
                    for j, (png_path, s, e) in enumerate(plist):
                        rs = max(s - ps, 0)
                        re_ = min(e - ps, pdur)
                        png_inputs += ["-i", png_path]
                        out = f"[s{j}]"
                        filter_parts.append(
                            f"{prev}[{j+1}:v]overlay="
                            f"x=(W-w)/2:y=H-h-60:"
                            f"enable='between(t,{rs:.3f},{re_:.3f})'{out}"
                        )
                        prev = out

                    # Add watermark (optional) and intro as inputs
                    if watermark_enabled:
                        wm_idx = len(png_inputs) // 2 + 1
                        png_inputs += ["-i", watermark_png]
                        # Watermark: top-right, entire segment
                        filter_parts.append(
                            f"{prev}[{wm_idx}:v]overlay="
                            f"x=W-w-20:y=20[wm]"
                        )
                        prev = "[wm]"

                    intro_idx = len(png_inputs) // 2 + 1
                    png_inputs += ["-i", intro_png]

                    # Intro: center, only if segment overlaps 0-10s
                    # (times relative to segment start)
                    intro_s = max(0 - ps, 0)
                    intro_e = min(10 - ps, pdur)
                    if intro_e > intro_s + 0.1:
                        filter_parts.append(
                            f"{prev}[{intro_idx}:v]overlay="
                            f"x=(W-w)/2:y=H-h-60:"
                            f"enable='between(t,{intro_s:.3f},{intro_e:.3f})'"
                            f"[vout]"
                        )
                    else:
                        filter_parts.append(f"{prev}null[vout]")

                    run_ffmpeg_simple([
                        "ffmpeg", "-y", "-hide_banner",
                        "-loglevel", "error",
                        "-i", str(raw_seg),
                        *png_inputs,
                        "-filter_complex", ";".join(filter_parts),
                        "-map", "[vout]", "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k",
                        "-avoid_negative_ts", "make_zero",
                        str(seg_out),
                    ], f"burn {idx} ({len(plist)} cues)")
                    raw_seg.unlink(missing_ok=True)

            # Concat all re-encoded segments
            update_status(job_id, status="encoding", progress=88)
            concat_list = seg_dir / "concat.txt"
            with open(concat_list, "w") as cf:
                for sf in seg_files:
                    cf.write(f"file '{sf}'\n")

            run_ffmpeg_simple([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ], "concat")

            # Cleanup
            shutil.rmtree(png_dir, ignore_errors=True)
            shutil.rmtree(seg_dir, ignore_errors=True)
            input_path.unlink(missing_ok=True)

            update_status(job_id, status="encoding", progress=90)

            output_size = upload_file_to_bunny(
                output_path,
                object_key,
                job_id,
            )

            update_status(
                job_id,
                status="done",
                progress=100,
                output_key=object_key,
                size=output_size,
                upload_bytes=output_size,
                upload_total=output_size,
            )

    except Exception as exc:
        try:
            delete_bunny_object(object_key)
        except Exception:
            pass

        update_status(
            job_id,
            status="error",
            progress=0,
            error=str(exc)[:1800],
        )

    finally:
        Path(subtitle_path).unlink(
            missing_ok=True
        )

        ass_path.unlink(
            missing_ok=True
        )

        output_path.unlink(
            missing_ok=True
        )



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


def list_bunny_outputs():
    if not bunny_is_configured():
        return []

    response = None

    try:
        response = requests.get(
            bunny_storage_url("outputs/"),
            headers=bunny_storage_headers(),
            timeout=(
                BUNNY_CONNECT_TIMEOUT,
                120,
            ),
        )

        if response.status_code == 404:
            return []

        if response.status_code != 200:
            raise RuntimeError(
                "Unable to list Bunny files: "
                f"HTTP {response.status_code} "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError(
                "Invalid Bunny file list response"
            )

        return data

    finally:
        if response is not None:
            response.close()


def cleanup_old_jobs():
    cutoff = (
        now_utc()
        - timedelta(hours=JOB_TTL_HOURS)
    )

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

            object_key = status_data.get(
                "output_key"
            )

            if object_key:
                try:
                    delete_bunny_object(
                        object_key
                    )
                except Exception:
                    continue

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )

            removed += 1

    if bunny_is_configured():
        for item in list_bunny_outputs():
            if item.get("IsDirectory"):
                continue

            object_name = str(
                item.get("ObjectName", "")
            ).strip()

            if not object_name:
                continue

            changed = parse_created_time(
                item.get("LastChanged")
                or item.get("DateCreated")
            )

            if changed is None or changed >= cutoff:
                continue

            delete_bunny_object(
                f"outputs/{object_name}"
            )

            removed += 1

    return removed



@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/submit", methods=["POST"])
def submit():
    if not bunny_is_configured():
        return jsonify({
            "error": (
                "Bunny storage is not configured. "
                "Add BUNNY_STORAGE_ZONE, "
                "BUNNY_STORAGE_PASSWORD, "
                "BUNNY_STORAGE_HOSTNAME and "
                "BUNNY_CDN_HOSTNAME in Railway."
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

    try:
        cleanup_old_jobs()
    except Exception:
        pass

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    # Watermark toggle (default on)
    watermark_enabled = request.form.get("watermark", "1") == "1"

    # Subtitle file is optional - watermark+intro always applied
    subtitle_file = request.files.get("subtitle")
    subtitle_path = None
    if subtitle_file and subtitle_file.filename:
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

        subtitle_path = str(job_dir / "subtitles.vtt")
        subtitle_file.save(subtitle_path)
    # No subtitle file = watermark + intro only

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
            watermark_enabled,
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

    object_key = status_data.get(
        "output_key"
    )

    if not object_key:
        return jsonify({
            "error": "Output object is missing"
        }), 404

    try:
        download_url = bunny_cdn_url(
            object_key
        )
    except Exception as exc:
        return jsonify({
            "error": (
                "Unable to create download link: "
                + str(exc)[:500]
            )
        }), 500

    return redirect(
        download_url,
        code=302,
    )



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
            delete_bunny_object(object_key)

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
