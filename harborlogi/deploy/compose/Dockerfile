# Build context: harborlogi/ (see compose.yaml). One image runs the web+executor process and the one-shot migration.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 DJANGO_SETTINGS_MODULE=harbor_web.settings
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[web]" \
    && useradd --system --uid 10001 --home-dir /app harbor \
    && mkdir -p /data /backup \
    && chown harbor:harbor /data /backup
USER harbor
# Waitress binds 127.0.0.1:8060 inside this container only; the proxy shares this network namespace.
CMD ["python", "-m", "harbor_web", "--port", "8060"]
