FROM python:3.12-slim

# Standard library only: nothing to pip install. The image carries the pipeline + HTTP API.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTIC_DATA_DIR=/data PORT=8080 HOST=0.0.0.0
WORKDIR /app
COPY agentic ./agentic
COPY run.py ./
COPY input ./input

# Unprivileged user; the only writable path is the data volume (run workspaces, checkpoints, releases).
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=20s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "agentic.api"]
