FROM python:3.12-slim AS builder

ENV MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

FROM python:3.12-alpine

# ffmpeg is used to repackage Telegram voice notes before uploading them
# to MAX: MAX rejects the OGG/Opus Telegram produces with
# AUDIO_VALIDATION_FAILED, so app/pymax_client.py remuxes the same Opus
# stream into the container MAX's own client records (see
# _voice_upload_variants). Without it voice messages still go out as-is,
# they just keep being rejected.
RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
    port=os.environ.get('HEALTH_PORT'); \
    sys.exit(0) if not port else sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
