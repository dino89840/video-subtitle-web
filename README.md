# Video Subtitle Burner

Web app that burns Burmese/English subtitles into videos. Deploy on Railway.

## Features

- Paste a remote video URL (direct mp4 link)
- Upload VTT or SRT subtitle file from your phone
- Background encoding — close the browser, it keeps running
- Download link when done (auto-deletes after 24 hours)
- Near-original quality (CRF 21), same resolution
- Correct Myanmar shaping (PIL + RAQM) + proper English font (no tofu blocks)
- Subtitles at standard cinema position (bottom)

## Deploy on Railway

1. Create a new GitHub repo and push this code
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select this repo — Railway auto-detects the Dockerfile
4. Wait for deploy, open the URL

No environment variables needed. Fonts and ffmpeg are in the Docker image.

## How it works

1. Downloads video from the URL you paste
2. Parses your VTT/SRT file
3. Renders each subtitle as PNG:
   - NotoSansMyanmar-Bold for Myanmar characters (U+1000–U+109F)
   - DejaVuSans-Bold for Latin/digits (per-character font selection)
   - Semi-transparent black box behind white text
4. Burns them in with ffmpeg overlay at 60px from bottom
5. Encodes H.264 CRF 21 (visually near-original)

## API

- `POST /api/submit` — form fields: `video_url`, `subtitle` (file)
- `GET /api/status/<job_id>` — `{"status","progress","error"}`
- `GET /api/download/<job_id>` — the finished mp4
- `POST /api/cleanup` — delete jobs older than 24h
