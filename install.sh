#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f config.env ]; then
    cp config.env.example config.env
    chmod 600 config.env
    echo "Created config.env: fill in GITHUB_TOKEN and GRAFANA_ADMIN_PASSWORD."
fi
set_key() { grep -q "^$1=" config.env && sed -i "s|^$1=.*|$1=$2|" config.env || echo "$1=$2" >> config.env; }
set_key DATA_DIR "$PWD/data"
set_key HOST_UID "$(id -u)"
set_key HOST_GID "$(id -g "$(id -un)")"
set_key DOCKER_GID "$(getent group docker | cut -d: -f3)"
ln -sfn config.env .env
mkdir -p data

docker build -t anyps5-gpu-ci-runner:latest runner
docker build -t anyps5-gpu-ci-poller:latest poller
echo "Built. Fill in config.env, then: docker compose up -d   (logs: docker compose logs -f poller)"
