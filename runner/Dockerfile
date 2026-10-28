FROM ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential gcc g++ cmake ninja-build ccache git python3 pkg-config file \
        libx11-dev libxext-dev libxrandr-dev libasound2-dev libpulse-dev libpipewire-0.3-dev \
        libwayland-dev libxkbcommon-dev libegl-dev libdecor-0-dev \
        libegl1 libxext6 libvulkan1 vulkan-tools vulkan-validationlayers mesa-vulkan-drivers \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN userdel -r ubuntu 2>/dev/null; groupadd -g 1000 ci && useradd -u 1000 -g 1000 -m ci

COPY run.sh /ci/run.sh
USER ci
ENTRYPOINT ["/bin/bash", "/ci/run.sh"]
