# Screenshot renderer for tap heatmaps. Build context: this directory.
#   docker build -t tgram-renderer renderer/
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
# Headless shell only (no full Chromium, no other browsers). --with-deps adds
# the system libraries and fonts (Latin, CJK, emoji) it needs.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY guard.py main.py ./

RUN useradd --no-create-home --shell /bin/false renderer
USER renderer

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
