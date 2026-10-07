"""Ядро конвейера: аудио -> транскрипт с диаризацией -> структурированный бриф -> TG.

Источник аудио не важен (путь A/B/C из docs/TZ.md) - на вход подаётся файл.

Транскрибация: gpt-4o-transcribe-diarize, /v1/audio/transcriptions,
response_format=diarized_json. У эндпоинта жёсткий лимит 25 МБ на файл,
поэтому длинное аудио режется по паузам и кодируется в mp3 32 kbps моно
(~100 минут на 25 МБ вместо ~13 минут у wav 16 кГц).

Использование:
    python3 brief.py meeting.wav --tg-chat -100123 --title "Планёрка"
    python3 brief.py meeting.wav --no-send      # только напечатать бриф
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import concurrent.futures as cf
import urllib.error
import urllib.request

OPENAI = "https://api.openai.com/v1"
STT_MODEL = "gpt-4o-transcribe-diarize"
LLM_MODEL = os.environ.get("LLM_MODEL") or "gpt-5-mini"
# 10 минут, а не 20: зависший запрос стоит целого куска работы, и чем кусок
# мельче, тем дешевле повтор. В 25 МБ лимита такой кусок укладывается с запасом.
CHUNK_SECONDS = 10 * 60
MIN_CHUNK_SECONDS = 2
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
STT_TIMEOUT = 600
STT_RETRIES = 3
# Параллелизм расшифровки: куски независимы. 4 хватает, чтобы двухчасовая
# встреча считалась минут пятнадцать вместо часа, и не упереться в лимиты.
STT_PARALLEL = int(os.environ.get("STT_PARALLEL", "4"))
MIN_WORDS = 25  # ниже этого брифовать нечего, см. main()

# Словарь для распознавания. Пополняется по мере того, как в стенограммах
# всплывают перевранные термины — задаётся через STT_VOCABULARY в .env.
DEFAULT_VOCABULARY = (
    "Встреча в Телемосте. Термины: бриф, ВКС, Телемост, Яндекс, планёрка, "
    "спринт, дедлайн, задача, релиз, стенограмма, транскрибация, VPS, деплой."
)
STT_VOCABULARY = os.environ.get("STT_VOCABULARY") or DEFAULT_VOCABULARY


VOICES = pathlib.Path(os.environ.get("VOICES_DIR", "/voices"))
# known_speaker_references принимает не больше четырёх образцов — жёсткий
# лимит API, не настройка проекта. Если знакомых голосов больше, берём тех,
# кого чаще узнавали: остальные останутся буквами.
MAX_KNOWN_SPEAKERS = 4


def known_speakers(src: pathlib.Path | None = None) -> tuple[list[str], list[str]]:
    """Имена и образцы голосов из банка, в виде data-URL для API.

    API берёт не больше четырёх образцов, а в банке людей больше. Раньше
    брались первые четыре по алфавиту — и 16.09 модель раздала реплики
    Сергея Юрьевича Алексею Крылову, которого на встрече не было: выбирать
    ей было не из кого. Теперь образцы берутся только для тех, кто есть
    в списке участников встречи. Нет совпадений — не отдаём ни одного:
    метка буквой честнее чужого имени.
    """
    if not VOICES.is_dir():
        return [], []
    import base64
    # У человека может быть несколько образцов, но API берёт по одному.
    # Выбираем самый свежий: в нём обычно лучший звук.
    people: dict[str, pathlib.Path] = {}
    for wav in sorted(VOICES.glob("*.wav")):
        people[wav.stem] = wav
    for d in sorted(VOICES.iterdir()):
        if d.is_dir() and not d.name.startswith(("_", ".")):
            picks = sorted(d.glob("*.wav"), key=lambda f: f.stat().st_mtime)
            if picks:
                people[d.name] = picks[-1]

    present = []
    plist = src.parent / f"{src.name}.participants.json" if src else None
    if plist and plist.exists():
        try:
            present = json.loads(plist.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            present = []
    # Сверяем имя и фамилию, а не просто слова. По одному имени нельзя:
    # тёзки встречаются чаще, чем кажется, и совпадения одного лишь имени
    # мало: образец чужого голоса получит имя однофамильца.
    # Порядок слов в Телемосте
    # любой («Крылов Алексей»), фамилия бывает сокращена до буквы
    # («Анастасия Н.»), ё и е пишут как попало.
    def norm(word: str) -> str:
        return word.lower().strip(".").replace("ё", "е")

    entries = [{norm(w) for w in str(n).split() if norm(w)} for n in present]

    def attends(name: str) -> bool:
        first, *rest = [norm(w) for w in name.split()]
        for tokens in entries:
            if first not in tokens:
                continue
            if not rest:
                return True  # в банке одно имя — сверять больше не с чем
            others = tokens - {first}
            for surname in rest:
                if surname in others:
                    return True
                if any(len(t) == 1 and surname.startswith(t) for t in others):
                    return True
        return False

    chosen = [name for name in sorted(people) if attends(name)]
    if not chosen:
        print("образцы голосов не отданы: никого из банка нет в списке "
              "участников — лучше буквы, чем чужие имена", flush=True)
        return [], []

    names, refs = [], []
    for name in chosen[:MAX_KNOWN_SPEAKERS]:
        data = base64.b64encode(people[name].read_bytes()).decode()
        names.append(name)
        refs.append(f"data:audio/wav;base64,{data}")
    print("образцы голосов для узнавания: " + ", ".join(names), flush=True)
    return names, refs


class Retryable(Exception):
    """Временная ошибка API: имеет смысл повторить тот же запрос."""


# Цены $/1М токенов на 2026-09-02. Держим таблицей, а не в голове: расход
# аккаунта смешан с другими проектами, и без своего учёта «куда ушли деньги»
# остаётся гаданием по графику в кабинете.
PRICES = {
    "gpt-4o-transcribe-diarize": (2.50, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.5": (5.00, 30.00),
    # Семейство 5.6 дешевело дважды: 30.07 срезали Terra и Luna, 21.08 — Sol.
    # Стартовые цены держать нельзя: по ним Luna считалась в 25 раз дороже,
    # чем стоит на самом деле, и сравнение моделей выходило кривым.
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
}
_usage: list[dict] = []
_usage_lock = threading.Lock()


def note_usage(model: str, resp: dict) -> None:
    u = (resp or {}).get("usage") or {}
    if not u:
        return
    # Поля называются по-разному у аудио- и текстовых эндпоинтов.
    inp = u.get("input_tokens") or u.get("prompt_tokens") or 0
    out = u.get("output_tokens") or u.get("completion_tokens") or 0
    # Неизвестная модель не бесплатная — она просто неизвестная. Молча
    # обнулять её расход значит врать в отчёте о деньгах.
    known = model in PRICES
    pin, pout = PRICES.get(model, (0.0, 0.0))
    with _usage_lock:
        _usage.append({"model": model, "input": inp, "output": out,
                       "usd": inp / 1e6 * pin + out / 1e6 * pout,
                       "priced": known})


def usage_summary() -> dict:
    with _usage_lock:
        by: dict[str, dict] = {}
        for r in _usage:
            m = by.setdefault(r["model"], {"input": 0, "output": 0, "usd": 0.0,
                                           "calls": 0})
            m["input"] += r["input"]
            m["output"] += r["output"]
            m["usd"] += r["usd"]
            m["calls"] += 1
        return {"by_model": by, "usd_total": round(sum(m["usd"] for m in by.values()), 4)}


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY не задан")
    return key


def post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{OPENAI}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def post_file(path: str, file: pathlib.Path, fields: dict) -> dict:
    """multipart/form-data без внешних зависимостей."""
    boundary = "----aibrief" + os.urandom(8).hex()
    body = bytearray()
    for k, v in fields.items():
        for item in (v if isinstance(v, list) else [v]):
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{item}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{file.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += file.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{OPENAI}{path}", data=bytes(body),
        headers={"Authorization": f"Bearer {api_key()}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        # 5xx и 429 — это «попробуй ещё», а не «запрос неверный». Раньше
        # любая HTTP-ошибка убивала весь прогон: одна случайная пятисотка
        # на одном куске из двенадцати обнуляла часовую работу.
        if e.code >= 500 or e.code == 429:
            raise Retryable(f"{e.code}: {body}") from e
        sys.exit(f"OpenAI {path} вернул {e.code}: {body}")


def duration_seconds(src: pathlib.Path) -> float:
    """Длительность записи.

    ffprobe отвечает «N/A», если в заголовке wav не проставлен размер —
    так бывает, когда запись обрывают на полуслове. Раньше это роняло
    весь конвейер, хотя само аудио читается прекрасно. Считаем тогда
    по размеру файла: 16 кГц, моно, 16 бит — ровно 32000 байт на секунду.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, check=True)
        return float(out.stdout.strip())
    except (ValueError, subprocess.CalledProcessError) as e:
        size = src.stat().st_size if src.exists() else 0
        by_size = max(0.0, (size - 44) / 32000)
        print(f"длительность из заголовка не прочиталась ({e}), "
              f"считаю по размеру: {by_size / 60:.1f} мин", flush=True)
        return by_size


def to_chunks(src: pathlib.Path, workdir: pathlib.Path) -> list[tuple[pathlib.Path, float]]:
    """Режем на куски по CHUNK_SECONDS и кодируем в mp3 32k моно (лимит 25 МБ)."""
    total = duration_seconds(src)
    chunks: list[tuple[pathlib.Path, float]] = []
    offset = 0.0
    idx = 0
    while offset < total:
        dst = workdir / f"chunk_{idx:03d}.mp3"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(offset), "-t", str(CHUNK_SECONDS), "-i", str(src),
             "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst)],
            check=True)
        if dst.stat().st_size > MAX_UPLOAD_BYTES:
            sys.exit(f"{dst} больше 25 МБ — уменьши CHUNK_SECONDS")
        # Когда длительность кратна размеру куска, последний выходит нулевым:
        # гонять через API 0.9 секунды тишины незачем.
        if duration_seconds(dst) >= MIN_CHUNK_SECONDS:
            chunks.append((dst, offset))
        offset += CHUNK_SECONDS
        idx += 1
    return chunks


def transcribe(src: pathlib.Path) -> list[dict]:
    """Возвращает сегменты [{speaker, text, start, end}] в таймлайне всей встречи."""
    names, refs = known_speakers(src)
    if names:
        print(f"знакомые голоса: {', '.join(names)}", flush=True)
    with tempfile.TemporaryDirectory() as td:
        chunks = to_chunks(src, pathlib.Path(td))
        total = len(chunks)
        done = 0
        lock = threading.Lock()

        def one(item: tuple[int, pathlib.Path, float]) -> list[dict]:
            nonlocal done
            n, chunk, offset = item
            resp: dict = {}
            for attempt in range(1, STT_RETRIES + 1):
                try:
                    fields = {
                        "model": STT_MODEL,
                        "response_format": "diarized_json",
                        "chunking_strategy": "auto",
                        # prompt сюда передать нельзя: «Prompt is not supported
                        # for diarization models». Словарь терминов поэтому
                        # уходит на этап саммари — см. STT_VOCABULARY в PROMPT.
                    }
                    if names:
                        fields["known_speaker_names[]"] = names
                        fields["known_speaker_references[]"] = refs
                    resp = post_file("/audio/transcriptions", chunk, fields)
                    note_usage(STT_MODEL, resp)
                    break
                except (Retryable, TimeoutError, OSError) as e:
                    print(f"  кусок {n}: попытка {attempt} не удалась: "
                          f"{type(e).__name__}: {e}", flush=True)
                    if attempt == STT_RETRIES:
                        raise SystemExit(
                            f"кусок {n} не расшифрован после {STT_RETRIES} попыток") from e
                    time.sleep(5 * attempt)
            out = [{
                "start": float(seg.get("start", 0)) + offset,
                "end": float(seg.get("end", 0)) + offset,
                "speaker": seg.get("speaker", "?"),
                "text": (seg.get("text") or "").strip(),
            } for seg in resp.get("segments", [])]
            with lock:
                done += 1
                # Прогресс по кускам: без него зависший запрос выглядел просто
                # как молчание процесса — 2026-09-01 это стоило 40 минут вслепую.
                print(f"расшифровано {done}/{total} (кусок {n}, с {hhmmss(offset)})",
                      flush=True)
            return out

        # Куски независимы, и гонять их по очереди — значит ждать час на
        # двухчасовой встрече. Порядок восстанавливаем сортировкой по offset,
        # а не по порядку, в котором вернулись ответы.
        work = [(i + 1, c, o) for i, (c, o) in enumerate(chunks)]
        print(f"кусков: {total}, параллельно: {min(STT_PARALLEL, total) or 1}", flush=True)
        with cf.ThreadPoolExecutor(max_workers=max(1, STT_PARALLEL)) as pool:
            segments = [s for part in pool.map(one, work) for s in part]

    segments.sort(key=lambda s: s["start"])
    return [s for s in segments if s["text"]]

def hhmmss(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcript_text(segments: list[dict]) -> str:
    return "\n".join(f"[{hhmmss(s['start'])}] {s['speaker']}: {s['text']}" for s in segments)


TRANSCRIPT_LINE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*([^:]{1,40}?):\s*(.*)$")


# GigaAM обучен на русской речи и английские названия записывает на слух:
# «Випин» вместо VPN. Замер на двухчасовой встрече: OpenAI узнал VPN 3 раза,
# GigaAM — 2 и один раз «Випин». Правим механически, до того как текст
# увидит модель саммари.
# Здесь только общие правки, годные любой команде. Правило отбора жёсткое:
# в список попадает лишь то, что подтверждается вторым употреблением термина
# в тех же записях; догадки о неизвестных названиях не включаются — связный,
# но выдуманный текст хуже явно битого. Слова вашей команды добавляйте в
# TERM_FIXES_EXTRA в .env (формат и пример — в .env.example), а не сюда.
TERM_FIXES = [
    # --- инструменты и модели
    (r"\bвипин\w*\b", "VPN"),
    (r"\bдипсик\w*\b", "DeepSeek"),
    (r"\bdipsik\b", "DeepSeek"),
    (r"\bdeep\s?sicke\b", "DeepSeek"),
    (r"\bджем[еи]н\w*\b", "Gemini"),
    (r"\bjamini\b", "Gemini"),
    (r"\bгрок\b", "Grok"),
    (r"\bсупер[-\s]?гро[кг]\w*\b", "SuperGrok"),
    (r"\bчат[-\s]?аджепти\b", "ChatGPT"),
    (r"\bчат[ыi]?\s?gpt\b", "ChatGPT"),
    (r"\bквн\b", "Qwen"),
    (r"\bми[-\s]?макон\b", "MiniMax"),
    (r"\bминимакс\b", "MiniMax"),

    # --- языки, утилиты, инфраструктура
    (r"\bпитон\b", "Python"),
    (r"\bпайтон\w*\b", "Python"),
    (r"\bpiton\b", "Python"),
    (r"\bкурл\b", "curl"),
    (r"\bвингет\b", "winget"),
    (r"\bгит\b", "Git"),
    (r"\bгитхаб\w*\b", "GitHub"),
    (r"\bсупербейст\w*\b", "Supabase"),
    (r"\bсупербо[йя]\w*\b", "Supabase"),
    (r"\bсупер[-\s]?бейс\w*\b", "Supabase"),
    (r"\bgitcap\b", "GitHub"),
    (r"\bлокалхост\w*\b", "localhost"),
    (r"\bуколохост\w*\b", "localhost"),
    (r"\bфорбидон\b", "Forbidden"),
    (r"\binstance[ыс]\b", "инстансы"),
    (r"\bинстанци[ияей]+\b", "инстансы"),
    (r"\bинсенс\w*\b", "инстансы"),

    # --- форматы файлов: GigaAM разбивает расширения на слоги
    (r"\bмаркдаун\w*\b", "Markdown"),
    (r"\bmark\s?dawn\b", "Markdown"),
    (r"\bdo[ck][-\s]?x\b", "DOCX"),
    (r"\bptt\b", "PPTX"),
    (r"\bash?tmil\b", "HTML"),
    (r"\bastml\b", "HTML"),
    (r"\bwarda\b", "Word"),
    (r"\bворд[ао]вск\w*\b", "Word"),

    # --- типичные описки распознавателя в английских словах
    (r"\boutpad\b", "output"),
    (r"\bimput\b", "input"),

    # --- прочее из живых записей
    (r"\bтелемост\w*\b", "Телемост"),
]


def parse_term_fixes(raw: str) -> list[tuple[str, str]]:
    """Правки терминов из .env: «шаблон=>замена», пары через «;;».

    Битая запись пропускается с сообщением: опечатка в регулярном выражении
    не должна валить расшифровку уже записанной встречи.
    """
    out: list[tuple[str, str]] = []
    for item in raw.split(";;"):
        item = item.strip()
        if not item:
            continue
        if "=>" not in item:
            print(f"TERM_FIXES_EXTRA: пропускаю {item[:40]!r} — нет «=>»", flush=True)
            continue
        pattern, right = item.split("=>", 1)
        try:
            re.compile(pattern.strip())
        except re.error as e:
            print(f"TERM_FIXES_EXTRA: пропускаю {pattern[:40]!r}: {e}", flush=True)
            continue
        out.append((pattern.strip(), right.strip()))
    return out


TERM_FIXES += parse_term_fixes(os.environ.get("TERM_FIXES_EXTRA", ""))


def fix_terms(segments: list[dict]) -> list[dict]:
    """Приводим english-термины к нормальному виду.

    Список пополняется по мере того, как в стенограммах всплывают новые
    перевранные названия — это дешевле, чем менять модель распознавания.
    """
    fixed = 0
    for seg in segments:
        text = seg["text"]
        for pattern, right in TERM_FIXES:
            text, n = re.subn(pattern, right, text, flags=re.IGNORECASE)
            fixed += n
        seg["text"] = text
    if fixed:
        print(f"поправлено терминов: {fixed}", flush=True)
    return segments


def parse_transcript(text: str) -> list[dict]:
    """Стенограмма обратно в сегменты — нужно, чтобы собрать один бриф по
    нескольким частям одной встречи, не переслушивая аудио заново."""
    segs = []
    for line in text.splitlines():
        m = TRANSCRIPT_LINE.match(line.strip())
        if not m:
            continue
        h, mi, s, spk, txt = m.groups()
        if txt.strip():
            start = int(h) * 3600 + int(mi) * 60 + int(s)
            segs.append({"start": start, "end": start, "speaker": spk.strip(),
                         "text": txt.strip()})
    return segs


BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "speakers", "tldr", "decisions", "tasks", "ideas",
                 "insights", "blockers", "numbers", "open_questions", "next_steps"],
    "properties": {
        "headline": {"type": "string"},
        "speakers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["label", "name", "evidence"],
            "properties": {"label": {"type": "string"}, "name": {"type": "string"},
                           "evidence": {"type": "string"}}}},
        "blockers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["who", "problem", "fix", "ts"],
            "properties": {"who": {"type": "string"}, "problem": {"type": "string"},
                           "fix": {"type": "string"}, "ts": {"type": "string"}}}},
        "numbers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["value", "about", "ts"],
            "properties": {"value": {"type": "string"}, "about": {"type": "string"},
                           "ts": {"type": "string"}}}},
        "tldr": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "quote", "ts"],
            "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                           "ts": {"type": "string"}}}},
        "tasks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["what", "owner", "due", "quote", "ts"],
            "properties": {"what": {"type": "string"},
                           "owner": {"type": "string"},
                           "due": {"type": "string"},
                           "quote": {"type": "string"}, "ts": {"type": "string"}}}},
        "ideas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "author", "quote", "ts"],
            "properties": {"text": {"type": "string"}, "author": {"type": "string"},
                           "quote": {"type": "string"}, "ts": {"type": "string"}}}},
        "insights": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "why_it_matters", "ts"],
            "properties": {"text": {"type": "string"},
                           "why_it_matters": {"type": "string"},
                           "ts": {"type": "string"}}}},
        "open_questions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "ts"],
            "properties": {"text": {"type": "string"}, "ts": {"type": "string"}}}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """Ты ведёшь протокол рабочей встречи. На входе — стенограмма с таймкодами
и метками спикеров.

Правила:
- Пиши только то, что реально прозвучало. Ничего не додумывай.
- Каждый пункт (кроме tldr и next_steps) обязан нести дословную цитату из
  стенограммы в поле quote и её таймкод в поле ts. Нет цитаты — не включай пункт.
- В tasks.owner пиши имя или метку спикера, кому поручено. Если не названо явно — "не назначен".
- В tasks.due пиши срок словами из встречи или "" если срок не обсуждался.
- Разделяй: decisions — что решили; tasks — что кто-то должен сделать;
  ideas — предложения без решения; insights — важные наблюдения о положении дел;
  open_questions — что осталось нерешённым.
- Язык брифа — русский.

- headline: одна фраза о том, зачем была встреча и чем кончилась. Не заголовок,
  а суть — её прочитают вместо всего брифа, если некогда.

- speakers: **сопоставь метки спикеров с настоящими именами.** Имена почти
  всегда есть в самих репликах: люди обращаются друг к другу («Иван, а можно
  я тоже скажу?»), прощаются по имени, представляются, делают перекличку.
  В evidence положи дословный фрагмент, по которому ты это понял. Если для
  метки такого подтверждения нет — не включай её вовсе. Догадки запрещены.
  Дальше по всему брифу используй найденное имя вместо буквы.
  Часть меток уже подписана именем — это узнавание по голосу, оно надёжнее
  догадки по тексту. Такие имена не переименовывай; в evidence для них
  напиши «узнан по голосу».

- blockers: у кого что не получилось и чем это лечили. Для обучений, внедрений
  и разборов это самая полезная часть: именно тут видно, кто отстал. Если на
  встрече никто ни на чём не застревал — оставь пустым, не выдумывай.

- numbers: **не больше пяти** самых весомых чисел встречи — деньги, сроки,
  количество людей, объёмы работ. Числа из пояснительных примеров («запрос
  стоит 100 токенов»), проценты остатка у одного участника и прочую мелочь
  не включай: длинный список цифр читать перестают. В about — чего число
  касается, коротко.

- tasks.owner: если исполнитель не назван поимённо, но по разговору очевидно,
  что это все участники или ведущий — так и пиши («все участники», «ведущий»).
  "не назначен" оставляй только когда действительно неясно.

- Названия, пути и каталоги распознаются особенно плохо: одно и то же может
  приехать несколькими написаниями. Если по смыслу это рабочее название и оно
  было продиктовано по буквам, пиши так, как его продиктовали, а не так, как
  услышал распознаватель. Не склеивай похожие по звучанию термины в
  несуществующие: если из контекста ясно, о чём речь, назови это прямо.
- Стенограмма машинная и содержит ошибки распознавания. Если слово явно
  перевранное, пиши в тексте пункта правильное (например, «грифт» — это
  «бриф»). В поле quote оставляй как есть, дословно из стенограммы.

Термины, которые встречаются на этих встречах:
""" + STT_VOCABULARY


def reasoning_effort() -> str:
    """Разные поколения принимают разные значения этого параметра:
    у gpt-5-mini минимум называется "minimal", у gpt-5.4 и новее — "none".
    Проверять до вызова, а не ловить 400."""
    gen = re.match(r"gpt-(\d+)\.(\d+)", LLM_MODEL)
    if gen and (int(gen.group(1)), int(gen.group(2))) >= (5, 4):
        return "none"
    return "minimal"


def summarize(segments: list[dict], participants: list[str] | None = None) -> dict:
    body = transcript_text(segments)
    if participants:
        # Имена из интерфейса встречи: диаризация даёт буквы, а кто эти буквы —
        # знает только Телемост. Модель сопоставляет их сама, но только там,
        # где по разговору это однозначно.
        body = ("Участники встречи по данным Телемоста: "
                + ", ".join(participants)
                + ".\nЕсли из разговора однозначно видно, какая метка кому "
                  "соответствует, подставляй имя. Если неоднозначно — оставляй "
                  "букву, догадки недопустимы.\n\n" + body)
    resp = post_json("/chat/completions", {
        "model": LLM_MODEL,
        "reasoning_effort": reasoning_effort(),
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": body},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "brief", "strict": True, "schema": BRIEF_SCHEMA}},
    })
    note_usage(LLM_MODEL, resp)
    return json.loads(resp["choices"][0]["message"]["content"])


def _norm(text: str) -> list[str]:
    keep = [c.lower() if c.isalnum() else " " for c in text]
    return "".join(keep).split()


def validate(brief: dict, segments: list[dict]) -> dict:
    """Выбрасываем пункты, чьих цитат нет в стенограмме, — фильтр против выдумок.

    Точное совпадение подстрокой не годится: модель нормализует пунктуацию и
    регистр, и честные цитаты отбраковывались. Считаем долю слов цитаты,
    встречающихся в стенограмме, и требуем 70% — выдумка так порог не проходит.
    """
    haystack = set(_norm(" ".join(s["text"] for s in segments)))
    dropped = 0
    for key in ("decisions", "tasks", "ideas", "insights"):
        kept = []
        for item in brief.get(key, []):
            words = _norm(item.get("quote") or "")
            hit = sum(1 for w in words if w in haystack) / len(words) if words else 0
            if words and hit >= 0.7:
                kept.append(item)
            else:
                dropped += 1
        brief[key] = kept
    brief["_dropped_unsupported"] = dropped
    return brief


def ts(item: dict) -> str:
    """Таймкод в скобках. Модель отдаёт его то как 00:11:36, то как
    [00:11:36] — снимаем её скобки, чтобы не печатать двойные."""
    raw = (item.get("ts") or "").strip().strip("[]")
    # Только начало: диапазон в каждой строке съедал ширину экрана.
    raw = raw.split("-")[0].split("–")[0].strip()
    return f" <i>{raw}</i>" if raw else ""


def render(brief: dict, title: str, meta: str = "") -> str:
    """Бриф под чтение в Телеграме.

    Эмодзи здесь не украшение, а якоря для глаза: бриф листают в ленте среди
    других сообщений, и заголовок без метки теряется. По одной на раздел.
    """
    L = ["📋 <b>" + title + "</b>"]
    if meta:
        L.append("<code>" + meta + "</code>")
    if brief.get("headline"):
        # Одна фраза для тех, кто дальше читать не будет.
        L += ["", "<blockquote>" + brief["headline"] + "</blockquote>"]

    named = [s for s in brief.get("speakers", []) if s.get("name")]
    if named:
        L += ["", "🎙 <b>Кто говорил</b>"]
        L += ["• <b>" + s["name"] + "</b> — метка " + s["label"] for s in named]

    if brief.get("tldr"):
        L += ["", "📌 <b>Коротко</b>"] + ["• " + x for x in brief["tldr"]]
    if brief.get("decisions"):
        L += ["", "⚖️ <b>Решения</b>"]
        L += ["• " + d["text"] + ts(d) for d in brief["decisions"]]
    if brief.get("tasks"):
        L += ["", "✅ <b>Задачи</b>"]
        for x in brief["tasks"]:
            due = " · срок: " + x["due"] if x.get("due") else ""
            L.append("• " + x["what"] + "\n   <b>" + (x.get("owner") or "не назначен")
                     + "</b>" + due + ts(x))
    if brief.get("blockers"):
        L += ["", "⚠️ <b>Где застряли</b>"]
        for b in brief["blockers"]:
            fix = " → " + b["fix"] if b.get("fix") else ""
            L.append("• <b>" + b["who"] + "</b>: " + b["problem"] + fix + ts(b))
    if brief.get("ideas"):
        L += ["", "💡 <b>Идеи</b>"]
        L += ["• " + i["text"] + (" — " + i["author"] if i.get("author") else "")
              for i in brief["ideas"]]
    if brief.get("insights"):
        L += ["", "🔍 <b>Инсайты</b>"]
        L += ["• " + i["text"] + " — " + i["why_it_matters"] for i in brief["insights"]]
    if brief.get("numbers"):
        L += ["", "🔢 <b>Цифры</b>"]
        L += ["• <b>" + n["value"] + "</b> — " + n["about"] for n in brief["numbers"]]
    if brief.get("open_questions"):
        L += ["", "❓ <b>Открытые вопросы</b>"]
        L += ["• " + q["text"] for q in brief["open_questions"]]
    if brief.get("next_steps"):
        L += ["", "📅 <b>Дальше</b>"] + ["• " + s for s in brief["next_steps"]]
    return "\n".join(L).strip()


def send_telegram(text: str, chat_id: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text,
                         "parse_mode": "HTML"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="аудиофайл или .txt-стенограмма с --from-transcript")
    ap.add_argument("--from-transcript", action="store_true",
                    help="на входе готовая стенограмма, расшифровку пропускаем")
    ap.add_argument("--from-segments", action="store_true",
                    help="на входе готовый segments.json вместо аудио")
    ap.add_argument("--title", default="Итоги встречи")
    ap.add_argument("--tg-chat")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--save", help="куда положить transcript.txt и brief.json")
    args = ap.parse_args()

    src = pathlib.Path(args.audio)
    if args.from_segments:
        # Готовые сегменты: расшифровка уже сделана снаружи.
        # Формат тот же, что пишем сами, поэтому дальше всё идёт как обычно.
        segments = json.loads(src.read_text(encoding="utf-8"))
        segments = [s for s in segments if (s.get("text") or "").strip()]
        total_sec = segments[-1]["end"] if segments else 0
        print(f"сегменты из {src.name}: {len(segments)}, "
              f"спикеров {len({s['speaker'] for s in segments})}")
        segments = fix_terms(segments)
    elif args.from_transcript:
        segments = parse_transcript(src.read_text(encoding="utf-8"))
        total_sec = segments[-1]["start"] if segments else 0
    else:
        segments = transcribe(src)
        total_sec = duration_seconds(src)
    print(f"Сегментов: {len(segments)}, длительность: {hhmmss(total_sec)}")

    # На почти пустой стенограмме модель всё равно что-нибудь сочинит —
    # проверено, выдала «Открытые вопросы: Hmm.» на трёх минутах тишины.
    # Тут честнее вообще не звать LLM.
    words = sum(len(s["text"].split()) for s in segments)
    if words < MIN_WORDS:
        text = (f"<b>{args.title}</b>\n\nВо встрече почти не было речи "
        # Длительность берём уже посчитанную: в режиме готовых сегментов
        # src — это json, и ffprobe на нём отвечает «N/A».         # из-за этого падал весь конвейер на короткой записи, хотя
        # расшифровка была готова и лежала рядом.
                f"({words} слов за {hhmmss(total_sec)}) — брифовать нечего.")
        if args.save:
            d = pathlib.Path(args.save)
            d.mkdir(parents=True, exist_ok=True)
            (d / "transcript.txt").write_text(transcript_text(segments), encoding="utf-8")
            (d / "brief.txt").write_text(text, encoding="utf-8")
        print(text)
        if args.tg_chat and not args.no_send:
            send_telegram(text, args.tg_chat)
        return 0

    # Имена участников кладёт рекордер рядом с аудио.
    plist = src.parent / f"{src.name}.participants.json"
    participants = []
    if plist.exists():
        try:
            participants = json.loads(plist.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            participants = []
    brief = validate(summarize(segments, participants), segments)

    u = usage_summary()
    print("расход: $" + format(u["usd_total"], ".3f") + " — "
          + ", ".join(f"{m}: {v['calls']} выз., ${v['usd']:.3f}"
                      for m, v in u["by_model"].items()), flush=True)

    # Шапка с фактами: длительность, число участников, число реплик. Читается
    # за секунду и сразу говорит, о какой встрече речь.
    meta = hhmmss(total_sec)
    if participants:
        meta += f" · {len(participants)} участн."
    meta += f" · {len(segments)} реплик"
    text = render(brief, args.title, meta)

    if args.save:
        d = pathlib.Path(args.save)
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.txt").write_text(transcript_text(segments), encoding="utf-8")
        # Сегменты с границами нужны, чтобы вырезать из записи чистый образец
        # голоса спикера: по стенограмме конец реплики не виден, а образцу
        # для known_speaker_references нужны ровные 2–10 секунд.
        (d / "segments.json").write_text(
            json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        (d / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        (d / "usage.json").write_text(
            json.dumps(usage_summary(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        (d / "brief.txt").write_text(text, encoding="utf-8")

    print(text)
    if brief["_dropped_unsupported"]:
        print(f"\n[отброшено пунктов без цитаты: {brief['_dropped_unsupported']}]")

    if args.tg_chat and not args.no_send:
        send_telegram(text, args.tg_chat)
        print("\nОтправлено в Telegram")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
