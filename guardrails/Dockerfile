# Guardrails & Execution Control service (policy, fairness, approval, audit, monitor).
#
# Hardening (with compose.guardrails.yml): non-root user, read-only filesystem except the
# data volume (action queue + hash-chained audit log), all Linux capabilities dropped,
# CPU / memory / process limits. Secrets come from guardrails/.env at run time, never the image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10002 guardrails \
 && useradd --system --uid 10002 --gid guardrails --no-create-home --shell /usr/sbin/nologin guardrails

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --only-binary=:all: -r requirements.txt -c constraints.txt

# Code and policy files only: no tests, no .env, no local database (.dockerignore).
COPY guardrails/ guardrails/
COPY config/ config/

RUN mkdir -p /data && chown guardrails:guardrails /data && chmod 700 /data

USER 10002:10002
ENV GUARDRAILS_DB_PATH=/data/guardrails.db
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4)"]

CMD ["uvicorn", "guardrails.settings:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8100", "--no-server-header"]
