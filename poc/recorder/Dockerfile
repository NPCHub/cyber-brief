FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      xvfb x11-utils \
      pulseaudio pulseaudio-utils \
      ffmpeg \
      python3 python3-venv python3-pip \
      ca-certificates curl procps \
      fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir playwright==1.49.1
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
COPY capture_selftest.py /app/capture_selftest.py
RUN chmod +x /app/entrypoint.sh

ENV DISPLAY=:99
ENTRYPOINT ["/app/entrypoint.sh"]
