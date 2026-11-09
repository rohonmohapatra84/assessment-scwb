FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SHORTENER_DB=/data/shortener.db
WORKDIR /app
COPY shortener ./shortener
COPY openapi.json schema.sql ./

# run as an unprivileged user; the only writable path is the data volume
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "shortener.server", "--host", "0.0.0.0", "--port", "8080"]
