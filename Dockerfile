# syntax=docker/dockerfile:1.7
# ---- 1) Tailwind CSS is compiled at build time; nothing is fetched from a CDN at runtime
FROM node:22.21-alpine3.22 AS assets
WORKDIR /build
RUN npm install --no-audit --no-fund --loglevel=error tailwindcss@4.3.4 @tailwindcss/cli@4.3.4
COPY assets ./assets
COPY elvin/templates ./elvin/templates
COPY elvin/static/js ./elvin/static/js
RUN npx @tailwindcss/cli -i assets/tailwind.css -o /build/app.css --minify

# ---- 2) the app (FastAPI + git + GitHub CLI + Locust)
FROM python:3.13-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 ELVIN_IN_DOCKER=1 HOME=/tmp
ARG GH_VERSION=2.103.3
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; arch="$(dpkg --print-architecture)"; cd /tmp; \
    curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt"; \
    grep " gh_${GH_VERSION}_linux_${arch}.tar.gz\$" "gh_${GH_VERSION}_checksums.txt" | sha256sum -c -; \
    tar -xzf "gh_${GH_VERSION}_linux_${arch}.tar.gz"; mv "gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh_*; gh --version
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY elvin ./elvin
COPY --from=assets /build/app.css ./elvin/static/css/app.css
RUN useradd -u 1000 -m elvin && mkdir -p /elvin && chown 1000:1000 /elvin
USER 1000
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=5 CMD curl -fs http://127.0.0.1:8765/healthz || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "elvin.main:app", "--host", "0.0.0.0", "--port", "8765", "--timeout-graceful-shutdown", "20", "--no-server-header"]
