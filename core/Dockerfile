# manifest-list digest captured 2026-08-02; update deliberately with
# the image-lock verification workflow rather than following a floating tag.
FROM python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl default-mysql-client \
    && groupadd --gid 2000 dpms-artifacts \
    && groupadd --gid 65532 dpms \
    && useradd --uid 65532 --gid 65532 --no-log-init \
       --home-dir /nonexistent --shell /usr/sbin/nologin dpms \
    && usermod --append --groups dpms-artifacts dpms \
    && rm -rf /var/lib/apt/lists/*

COPY core/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY VERSION.md /app/VERSION.md
COPY shared /app/shared
COPY core/app /app/app
COPY core/migrations /app/migrations

ENV HOME=/tmp/dpms-home
USER 65532:65532

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
