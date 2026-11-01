# One-shot loader for the synthetic HR dataset. Stdlib only, no pip installs.
FROM python:3.12-slim
WORKDIR /seed
COPY seed.py generate.py ./
COPY dataset ./dataset
COPY branding ./branding
USER nobody
ENTRYPOINT ["python3", "-u", "/seed/seed.py"]
