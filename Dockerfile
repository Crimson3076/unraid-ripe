FROM python:3.12-slim

LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/Crimson3076/unraid-ripe/main/ripe/static/icon.png"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RIPE_CONFIG_DIR=/config \
    TZ=UTC

WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock
COPY --chown=99:100 ripe ./ripe
RUN mkdir -p /config \
    && chown 99:100 /config

# The deployment adds only the socket's group, never privileged mode.
USER 99:100
EXPOSE 8087
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8087/health', timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "ripe.app:app", "--host", "0.0.0.0", "--port", "8087", "--no-access-log"]
