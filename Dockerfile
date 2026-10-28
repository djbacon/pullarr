FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.13-slim

ARG APP_VERSION=0.16.1
LABEL org.opencontainers.image.title="Pullarr" \
      org.opencontainers.image.version="$APP_VERSION"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/config/state.db

COPY --from=deno /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates coreutils util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py plex.py ytdlp_runtime.py channel_assets.py video_encoding.py ./

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /config /downloads \
    && chown -R app:app /app /config /downloads

USER app

CMD ["python", "/app/app.py"]
