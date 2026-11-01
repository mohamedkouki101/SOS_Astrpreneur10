# Untrusted Input Zone: ingestion and sanitization service.
#
# Hardening (with compose.ingest.yml): non-root user, read-only filesystem
# except the evidence volume and a small /tmp, all Linux capabilities
# dropped, CPU / memory / process limits. Secrets come from the environment
# at run time and are never copied into the image.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged user: no login shell, no home directory.
RUN groupadd --system --gid 10001 ingest \
 && useradd --system --uid 10001 --gid ingest --no-create-home --shell /usr/sbin/nologin ingest

WORKDIR /app

# Ready-built packages only: nothing is compiled during the build.
COPY requirements.txt .
RUN pip install --only-binary=:all: -r requirements.txt

# Only the service code goes in: no tests, no .env, no local notes (.dockerignore).
COPY ingest/ ingest/

# The only writable place: the Evidence Store volume, owner-only.
RUN mkdir -p /data/evidence && chown ingest:ingest /data/evidence && chmod 700 /data/evidence

USER 10001:10001
ENV EVIDENCE_DIR=/data/evidence
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4)"]

CMD ["uvicorn", "ingest.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-server-header"]
