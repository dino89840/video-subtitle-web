FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto \
    fonts-dejavu \
    fonts-sil-padauk \
    ca-certificates \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/video-subtitle-jobs

ENV PORT=5000
ENV JOBS_DIR=/tmp/video-subtitle-jobs
ENV SUBTITLE_FONT_NAME=Padauk
ENV FONTS_DIR=/usr/share/fonts
ENV JOB_TTL_HOURS=24
ENV DOWNLOAD_URL_EXPIRES=21600
ENV MAX_SOURCE_GB=10
ENV MAX_CONCURRENT_JOBS=1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 --graceful-timeout 30 app:app"]
