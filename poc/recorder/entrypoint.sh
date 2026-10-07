#!/usr/bin/env bash
# Общий бутстрап звукового стенда: X-сервер + виртуальная звуковая карта.
# Всё, что играет Chromium, попадает в vsink; ffmpeg пишет vsink.monitor.
set -euo pipefail

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for i in $(seq 1 30); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.2; done

pulseaudio --start --exit-idle-time=-1 --disallow-exit -n \
  --load="module-native-protocol-unix" \
  --load="module-null-sink sink_name=vsink sink_properties=device.description=vsink" \
  --load="module-null-sink sink_name=vmic sink_properties=device.description=vmic" \
  --load="module-virtual-source source_name=vsource master=vmic.monitor"
pactl set-default-sink vsink
pactl set-default-source vsink.monitor

exec "$@"
