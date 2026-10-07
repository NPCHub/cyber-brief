"""PoC 1: доказать, что звук из браузера в контейнере реально пишется на диск.

Chromium (не headless, под Xvfb — как у продуктовых meeting-ботов) открывает
локальную страницу с <audio autoplay>, звук уходит в виртуальную карту vsink,
ffmpeg параллельно пишет vsink.monitor в wav 16 кГц моно — ровно тот формат,
который дальше уходит в STT.
"""
import http.server, os, pathlib, subprocess, sys, threading, wave, contextlib

SAMPLE = pathlib.Path(sys.argv[1]).resolve()      # исходный аудиофайл
OUT = pathlib.Path(sys.argv[2]).resolve()          # куда писать запись
SECONDS = int(sys.argv[3]) if len(sys.argv) > 3 else 20

root = SAMPLE.parent
page = root / "_player.html"
page.write_text(
    f'<!doctype html><meta charset="utf-8"><body>'
    f'<audio id="a" src="{SAMPLE.name}" autoplay></audio>'
    f'<script>document.getElementById("a").play()</script>',
    encoding="utf-8",
)

os.chdir(root)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

rec = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "pulse", "-i", "vsink.monitor",
     "-ac", "1", "-ar", "16000", "-t", str(SECONDS), str(OUT)]
)

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium",
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--autoplay-policy=no-user-gesture-required",
              "--use-fake-ui-for-media-stream"],
    )
    pg = browser.new_page()
    pg.goto("http://127.0.0.1:8765/_player.html")
    pg.wait_for_timeout(SECONDS * 1000)
    browser.close()

rec.wait(timeout=60)

with contextlib.closing(wave.open(str(OUT), "rb")) as w:
    frames = w.readframes(w.getnframes())
peak = max(abs(int.from_bytes(frames[i:i+2], "little", signed=True)) for i in range(0, len(frames), 2))
print(f"RESULT file={OUT} bytes={OUT.stat().st_size} peak_amplitude={peak}")
print("VERDICT:", "AUDIO CAPTURED" if peak > 500 else "SILENCE — capture broken")
