# Railway Dockerfile - includes ffmpeg + Myanmar fonts
FROM python:3.12-slim

# Install ffmpeg and fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto \
    fonts-dejavu \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/share/fonts/truetype/padauk \
    && wget -q -O /usr/share/fonts/truetype/padauk/Padauk-Bold.ttf "https://github.com/silnrsi/font-padauk/raw/master/fonts/Padauk-Bold.ttf" \
    && wget -q -O /usr/share/fonts/truetype/padauk/Padauk-Regular.ttf "https://github.com/silnrsi/font-padauk/raw/master/fonts/Padauk-Regular.ttf" \
    && fc-cache -f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verify fonts exist (fallback paths)
RUN ls /usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf \
       /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
    || (echo "WARNING: fonts not found" && fc-list | grep -i myanmar | head -3)

ENV PORT=5000
ENV FONT_MM=/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf
ENV FONT_LAT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 3600 app:app"]
