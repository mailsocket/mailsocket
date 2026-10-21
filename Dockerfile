# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && apt-get purge -y build-essential libpq-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY backend/ /app/backend/
COPY deploy/entrypoint.sh /entrypoint.sh
COPY deploy/provision-postfix-role.py /usr/local/bin/provision-postfix-role
RUN chmod +x /entrypoint.sh \
    && chmod 0755 /usr/local/bin/provision-postfix-role \
    && mkdir -p /app/backend/staticfiles \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app \
    && chown -R app:app /app

USER app
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
# Shell form so the same GUNICORN_WORKERS / GUNICORN_THREADS env vars that
# config.settings.base reads size the process (docker-compose.yml overrides
# this with the identical command; defaults pinned by
# test_wait_capacity_headroom).
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --worker-class gthread --workers ${GUNICORN_WORKERS:-3} --threads ${GUNICORN_THREADS:-32} --worker-connections ${GUNICORN_THREADS:-32} --timeout 120 --max-requests 1000 --max-requests-jitter 50 --access-logfile - --error-logfile -"]
