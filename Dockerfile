FROM python:3.12-slim AS builder

ENV MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

FROM python:3.12-alpine

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
    port=os.environ.get('HEALTH_PORT'); \
    sys.exit(0) if not port else sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
