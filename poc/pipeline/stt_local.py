"""Self-hosted STT: доказательство транскрибации без внешних API и без биллинга.
Тот же интерфейс сегментов, что и у облачного пути в brief.py."""
import sys, time
from faster_whisper import WhisperModel

src, size = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "small")
t0 = time.time()
model = WhisperModel(size, device="cpu", compute_type="int8", download_root="/models")
load = time.time() - t0

t1 = time.time()
segments, info = model.transcribe(src, language="ru", vad_filter=True)
out = [(s.start, s.end, s.text.strip()) for s in segments]
dec = time.time() - t1

print(f"model={size} load={load:.1f}s decode={dec:.1f}s audio={info.duration:.1f}s rtf={dec/info.duration:.2f}")
for a, b, t in out:
    print(f"[{a:6.2f}-{b:6.2f}] {t}")
