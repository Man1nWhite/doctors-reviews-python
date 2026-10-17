# Both stages use the same Python/OS so the copied virtualenv stays compatible.
FROM python:3.13-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME=/opt/browser-cache

# ---- build stage: runtime dependencies and the Linux browser profile ----
FROM base AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 TQDM_DISABLE=1
WORKDIR /app
COPY requirements.txt .

RUN python3 -m venv venv \
 && venv/bin/pip install --no-cache-dir --no-compile --only-binary=:all: -r requirements.txt \
 && venv/bin/pip check

# Pin the browser independently of the Python wrapper; do not fetch "latest".
COPY docker/fetch_camoufox.py /tmp/fetch_camoufox.py
RUN venv/bin/python /tmp/fetch_camoufox.py \
 && find /app/venv -type d -name __pycache__ -prune -exec rm -rf '{}' +

# ---- runtime stage: minimal image with only what's needed ----
FROM base AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        xvfb xauth \
        libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdbus-1-3 libdbus-glib-1-2 libdrm2 libgtk-3-0 libgbm1 libasound2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxshmfence1 \
        libxkbcommon0 libxtst6 \
 && rm -rf /var/lib/apt/lists/*

# Only Linux fonts are packaged: keep the generated fingerprint consistent.
ENV BROWSER_OS=linux \
    PATH="/app/venv/bin:$PATH" \
    DISPLAY=:99

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/data /app/screenshots /opt/browser-cache /tmp/.X11-unix \
 && chown appuser:appuser /app /app/data /app/screenshots /opt/browser-cache \
 && chmod 1777 /tmp/.X11-unix

COPY --from=builder --chown=appuser:appuser /app/venv ./venv
COPY --from=builder --chown=appuser:appuser /opt/browser-cache /opt/browser-cache

COPY --chown=appuser:appuser main.py sentiment_service.py index.html favicon.ico check_access.py .env.example ./
COPY --chown=appuser:appuser collectors ./collectors
COPY --chown=appuser:appuser app ./app

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["/app/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health')"]

CMD ["sh", "-c", "Xvfb :99 -screen 0 1365x900x24 -nolisten tcp & sleep 1; exec /app/venv/bin/python /app/main.py"]
