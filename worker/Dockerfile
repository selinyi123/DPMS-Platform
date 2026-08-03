# manifest-list digest captured 2026-08-02; the Python package lock
# and Playwright browser bundle are intentionally released together.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy@sha256:c257db7f706f31f852740b039dea4f4611418f1a6816c0ceae716dd20d4eddda

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl default-mysql-client \
    && groupadd --gid 2000 dpms-artifacts \
    && usermod --append --groups dpms-artifacts pwuser \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY shared /app/shared
COPY core/migrations /app/migrations
COPY worker/app /app/app

ENV HOME=/home/pwuser
USER 1000:1000

CMD ["python", "-m", "app.main"]
