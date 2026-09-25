# WWP Launch Radar - Linux container image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WWP_DATA_DIR=/data \
    WWP_HOST=0.0.0.0 \
    WWP_PORT=8765

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY wwp_radar ./wwp_radar

RUN useradd --system --uid 10001 --home-dir /app radar \
    && mkdir -p /data && chown radar:radar /data
USER radar
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WWP_PORT', '8765'), timeout=4)"]

# Exec form: the Python process receives SIGTERM directly and shuts down gracefully
# (collector threads stopped, lock released, SQLite connections closed).
STOPSIGNAL SIGTERM
CMD ["python", "-m", "wwp_radar", "serve"]
