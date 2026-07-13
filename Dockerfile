# Flip Board production image — Python + Playwright + Chromium
FROM python:3.11-slim

# Playwright needs system deps for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 libcairo2 libcups2 \
    libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libgcc1 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 \
    libstdc++6 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # PyWebView isn't needed on the server (no GUI), strip it out
    pip uninstall -y pywebview || true

# Install Playwright's Chromium browser, plus patchright's (Scrapling's
# stealth engine — same 1.61 revision, but the explicit install makes the
# stealth fallback deterministic instead of relying on a shared cache).
RUN playwright install chromium && \
    patchright install chromium

# App code (uvicorn entry point is backend.server:app — no top-level server.py needed)
COPY backend ./backend
COPY frontend ./frontend

# Render mounts persistent disk at /var/data — link our data dir there.
# (Render free tier doesn't include disks; on paid you'll set disk at deploy.)
RUN mkdir -p /var/data
ENV FLIPBOARD_DATA_DIR=/var/data

EXPOSE 8765

# Render injects $PORT — bind to it (fallback 8765 for local docker run)
CMD ["sh", "-c", "uvicorn backend.server:app --host 0.0.0.0 --port ${PORT:-8765}"]
