# CallingBot container image: the web app (default command) and, with a different command,
# the dialer (see docker-compose.yml).
#
#   docker build -t callingbot .
#   docker run --env-file .env -p 8000:8000 callingbot
#
# Only the paths the app needs are copied (never the whole checkout), so a local .env,
# SQLite database or distributor CSV can never end up inside the image.
FROM python:3.11-slim

# DATABASE_URL defaults to a SQLite file in a writable folder (mount a volume on /app/var to keep
# it). Production should point DATABASE_URL at Postgres; docker-compose.yml does that for you.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite:////app/var/callingbot.db

WORKDIR /app

# Dependencies and package first, so editing config/ does not reinstall everything.
COPY pyproject.toml README.md ./
COPY callingbot ./callingbot
RUN pip install --no-cache-dir ".[postgres]" \
    && rm -rf build ./*.egg-info

# Business content (AMC, NFO, FAQ, calling policy). CONFIG_DIR defaults to ./config.
COPY config ./config

# Run as an unprivileged user. Code and config stay root-owned (read-only for the app);
# the only writable place is /app/var.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin callingbot \
    && mkdir -p /app/var \
    && chown callingbot:callingbot /app/var
USER callingbot

EXPOSE 8000

CMD ["uvicorn", "--factory", "callingbot.web.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
