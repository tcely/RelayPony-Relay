# syntax=docker/dockerfile:1
# check=error=true

FROM python:3.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --no-create-home app

RUN pip install --upgrade pip uv

USER app
WORKDIR /app
COPY . .

RUN uv pip install -r requirements.in && \
    uv venv .venv && \
    . .venv/bin/activate && \
    uv pip install ruff rust-just

EXPOSE 8000

ENTRYPOINT ["gunicorn", "relay.wsgi:application", "--access-logfile", "-"]
CMD ["--workers", "4", "--bind", "0.0.0.0:8000", ]

