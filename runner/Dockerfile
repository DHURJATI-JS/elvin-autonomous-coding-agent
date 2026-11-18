# Isolated sandbox: runs approved commands on a throw-away copy of a project and
# renders pages in network-blocked Chromium. It holds no secrets and cannot reach
# Elvin's database.
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm git ca-certificates tini curl \
 && rm -rf /var/lib/apt/lists/*
RUN pip install fastapi==0.162.0 uvicorn==0.54.0 python-multipart==0.0.33 playwright==1.64.1 pytest==9.2.0 \
 && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
RUN useradd -u 1001 -m sandbox && mkdir -p /jobs /cache && chown 1001:1001 /jobs /cache
COPY runner/server.py /srv/server.py
USER 1001
EXPOSE 9000
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "server:app", "--app-dir", "/srv", "--host", "0.0.0.0", "--port", "9000", "--no-server-header"]
