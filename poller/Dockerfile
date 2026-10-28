FROM docker:29-cli AS docker

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir prometheus-client==0.21.1
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY poller.py report.py stats.py seccomp.json /app/
WORKDIR /app
ENV HOME=/tmp PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/poller.py"]
