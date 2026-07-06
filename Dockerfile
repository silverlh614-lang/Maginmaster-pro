FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    DATA_DIR=/data

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
COPY knowledge ./knowledge

# Persistence: bot run-state, journal, and the live position + compounded
# equity all live under DATA_DIR. Mount a Railway Volume at /data so they
# survive restarts/redeploys. Without a mounted volume /data is ephemeral
# (wiped on every deploy) and nothing is remembered.
RUN mkdir -p /data
VOLUME ["/data"]

# Railway injects PORT at runtime
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
