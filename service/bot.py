"""Телеграм-обвязка: кидаешь ссылку на Телемост — получаешь бриф в этот же чат.

Живёт systemd-юнитом прямо на VPS и дёргает docker для тяжёлой работы:
рекордер и конвейер расшифровки запускаются одноразовыми контейнерами.
Long polling, а не вебхук — не нужен ни домен, ни TLS, ни открытый порт.

Только стандартная библиотека: на боксе 2 ядра и 4 ГБ, лишние зависимости
здесь стоят дороже, чем удобство.

    /brief <ссылка> [минут]   привести бота на встречу и прислать бриф
    /stop                     закончить текущую запись досрочно
    /status                   что сейчас происходит

Настройки лежат в файле .env в каталоге установки (AI_BRIEF_HOME),
полный список с пояснениями — в .env.example:
    TELEGRAM_BOT_TOKEN   токен бота
    OPENAI_API_KEY       ключ OpenAI: расшифровка без gpu-hub и резерв
    LLM_BASE_URL         OpenAI-совместимый сервер для брифа (пусто — OpenAI)
    LLM_API_KEY          его ключ (пусто — OPENAI_API_KEY)
    ALLOWED_CHATS        chat_id через запятую; пусто — бот никого не обслуживает
    OWNER_IDS            кто может привязывать чаты командой /link;
                         пусто — владельцами считаются личные чаты из ALLOWED_CHATS
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import sys
import hashlib
import hmac
import html
import http.client
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

# Каталог установки. Раньше путь был зашит под один сервер; теперь берём из
# AI_BRIEF_HOME, а если не задан — каталог репозитория (на уровень выше service/).
# Юнит systemd, который генерирует install.sh, задаёт AI_BRIEF_HOME явно.
ROOT = pathlib.Path(os.environ.get("AI_BRIEF_HOME")
                    or pathlib.Path(__file__).resolve().parent.parent).resolve()


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Читаем .env сразу при импорте, а не в main(): BOT_NAME и YANDEX_ACCOUNT
# ниже считываются на уровне модуля, и до этого .env ещё не был прочитан —
# значения из файла молча игнорировались.
load_env()

JOBS = ROOT / "jobs"
VOICES = ROOT / "voices"
# Профиль браузера с сессией Яндекса: из него Телемост берёт имя
# и аватар бота. Без сессии всё работает как раньше, гостем.
PROFILE = ROOT / "profile"
# К одному номеру телефона привязано несколько аккаунтов, и вход
# по QR тянет в браузер их все. Какой из них рабочий — говорим явно.
YANDEX_ACCOUNT = os.environ.get("YANDEX_ACCOUNT", "")
RECORDER_IMAGE = "ai-brief-recorder:poc"
# Имя, под которым бот заходит на встречу и которое видят участники.
# Раньше было зашито в рекордере; в .env меняется без правки кода.
BOT_NAME = os.environ.get("BOT_NAME") or "Cyber Brief"
TELEMOST_RE = re.compile(r"https://telemost\.yandex\.ru/j/\d+")
# Обучение и стратсессии идут дольше планёрки, а тихий обрыв на
# потолке выглядит как поломка. Уйти раньше бот умеет сам.
DEFAULT_MAX_MIN = 240

API = "https://api.telegram.org/bot{}/{}"


def log(*parts: object) -> None:
    """Лог с меткой времени: разбор инцидента 2026-09-01 упёрся в то,
    что строки в /var/log/ai-brief-bot.log были без времени."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


# ---------------------------------------------------------------- telegram

# Связь с api.telegram.org с этого VPS рвётся: за 3 сентября 24 обрыва,
# все на этапе установки TLS-соединения. Пока попытка висела на общем
# таймауте, сообщение лежало в очереди у Телеграма — со стороны это выглядит
# как «бот проснулся через полторы минуты и разом ответил на всё».
# Поэтому таймаут на соединение отдельный и короткий: не отвечают за пять
# секунд — сразу пробуем заново, а не ждём вместе с пользователем.
CONNECT_TIMEOUT = 5
CONNECT_TRIES = 3


def tg_once(method: str, payload: dict, token: str, read_timeout: int) -> dict:
    """Один запрос: соединение под коротким таймаутом, чтение под длинным."""
    conn = http.client.HTTPSConnection("api.telegram.org",
                                       timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
        # Соединение установлено — дальше ждём ответ сколько нужно:
        # getUpdates по своей природе висит до полусотни секунд.
        conn.sock.settimeout(read_timeout)
        conn.request("POST", f"/bot{token}/{method}",
                     json.dumps(payload).encode(),
                     {"Content-Type": "application/json"})
        # Телеграм и на ошибку отвечает телом с ok=false, поэтому код
        # состояния отдельно не разбираем.
        return json.load(conn.getresponse())
    finally:
        conn.close()


def tg(method: str, payload: dict, token: str) -> dict:
    # getUpdates висит долго по своей природе, остальным методам столько не
    # нужно: одна залипшая отправка на 90 секунд морозила весь бот, и запрос
    # имени голоса просто не доходил до чата.
    read_timeout = 70 if method == "getUpdates" else 20
    last = "?"
    for attempt in range(1, CONNECT_TRIES + 1):
        try:
            return tg_once(method, payload, token, read_timeout)
        except Exception as e:  # noqa: BLE001
            # Ловим всё. Обрыв связи раньше пробивал наружу через poll()
            # и убивал процесс целиком — systemd поднимал бота, но потоки
            # идущих задач умирали вместе с ним, и записанная встреча
            # оставалась без брифа. 2026-09-01, дважды.
            last = f"{type(e).__name__}: {e}"
            if attempt < CONNECT_TRIES:
                time.sleep(1)
    return {"ok": False, "error": f"{last} (попыток: {CONNECT_TRIES})"}


# В группах с темами каждое сообщение принадлежит ветке. Телеграм не
# подставляет её сам: сообщение без указания темы падает в «Общий»,
# что и случилось 05.10 — историю спросили в «Песочнице», а брифы
# пришли не туда. Помним последнюю тему каждого чата и подставляем её
# во все исходящие.
THREADS: dict[int, int] = {}


def remember_thread(chat: int, thread: int | None) -> None:
    if thread:
        THREADS[chat] = thread
    else:
        THREADS.pop(chat, None)


def thread_of(chat: int) -> dict:
    """Кусок запроса с темой — пустой, если чат без тем."""
    tid = THREADS.get(chat)
    return {"message_thread_id": tid} if tid else {}


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Режем длинный бриф по строкам, а не по счётчику символов.

    Слепой разрез каждые 3900 знаков рано или поздно приходится внутрь
    тега разметки, и Телеграм отказывается разбирать такое сообщение
    целиком: «can't find end tag». из-за этого не дошёл бриф
    на полуторачасовую встречу. Строки в брифе короткие, поэтому резать
    по ним безопасно.
    """
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            parts.append(current)
            current = ""
        # Одна строка длиннее предела — рубим её, деваться некуда.
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        parts.append(current)
    return parts


def send(chat: int, text: str, token: str, reply_to: int | None = None) -> None:
    # Телеграм режет сообщения на 4096 символов — длинный бриф шлём частями.
    for n, part in enumerate(split_message(text)):
        r = tg("sendMessage", {"chat_id": chat, "text": part,
                               "parse_mode": "HTML", **thread_of(chat),
                               **({"reply_to_message_id": reply_to}
                                  if reply_to and n == 0 else {})},
               token)
        if r.get("ok"):
            continue
        # Молчание вместо ответа — худший вид отказа: пользователь видит, что
        # бот «не работает», а в логе пусто. Так и вышло с /status: Телеграм
        # отклонял сообщение из-за сломанного HTML, а мы не смотрели на ответ.
        log("sendMessage не прошёл:",
            str(r.get("error") or r.get("description"))[:200])
        plain = re.sub(r"<[^>]+>", "", part)
        fallback = tg("sendMessage", {"chat_id": chat, "text": plain,
                                      **thread_of(chat)}, token)
        if not fallback.get("ok"):
            log("и без разметки тоже:",
                str(fallback.get("error") or fallback.get("description"))[:200])


def send_kb(chat: int, text: str, rows: list, token: str,
            reply_to: int | None = None) -> None:
    r = tg("sendMessage", {
        "chat_id": chat, "text": text, "parse_mode": "HTML", **thread_of(chat),
        **({"reply_to_message_id": reply_to} if reply_to else {}),
        "reply_markup": {"inline_keyboard": rows},
    }, token)
    if not r.get("ok"):
        log("клавиатура не отправилась:",
            str(r.get("error") or r.get("description"))[:200])


def send_document(chat: int, path: pathlib.Path, caption: str, token: str) -> None:
    # Пустой файл Телеграм отклоняет («file must be non-empty»), а пустая
    # стенограмма — нормальный итог тихой записи, не ошибка.
    if not path.exists() or path.stat().st_size == 0:
        log("пустая стенограмма, не отправляю:", path.name)
        return
    boundary = "----aibrief" + os.urandom(8).hex()
    body = bytearray()
    pairs = [("chat_id", str(chat)), ("caption", caption)]
    if THREADS.get(chat):
        pairs.append(("message_thread_id", str(THREADS[chat])))
    for k, v in pairs:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{path.name}\"\r\nContent-Type: text/plain\r\n\r\n").encode()
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API.format(token, "sendDocument"), data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        log("sendDocument failed:", e.read()[:200])


def send_voice(chat: int, path: pathlib.Path, caption: str,
               token: str, markup: dict | None = None) -> None:
    # Не вырезалось — молчим, но кнопку для этой метки всё равно покажем.
    if not path.exists() or path.stat().st_size == 0:
        log("пустой образец, не отправляю:", path.name)
        return
    boundary = "----aibrief" + os.urandom(8).hex()
    body = bytearray()
    fields = [("chat_id", str(chat)), ("caption", caption)]
    tid = THREADS.get(chat)
    if tid:
        fields.append(("message_thread_id", str(tid)))
    if markup:  # кнопка «Это…» прямо под голосовым
        fields.append(("reply_markup", json.dumps(markup, ensure_ascii=False)))
    for k, v in fields:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; "
             f"filename=\"{path.name}\"\r\nContent-Type: audio/ogg\r\n\r\n").encode()
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API.format(token, "sendVoice"), data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        log("sendVoice failed:", e.read()[:200])


def send_photo(chat: int, path: pathlib.Path, caption: str, token: str) -> None:
    """Картинкой, а не файлом: QR-код наводят телефоном."""
    boundary = "----aibrief" + os.urandom(8).hex()
    body = bytearray()
    pairs = [("chat_id", str(chat)), ("caption", caption)]
    if THREADS.get(chat):
        pairs.append(("message_thread_id", str(THREADS[chat])))
    for k, v in pairs:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
             f"filename=\"{path.name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API.format(token, "sendPhoto"), data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        log("sendPhoto failed:", e.read()[:200])


def session_state() -> dict:
    """Что известно про вход в Яндекс. Пустой словарь — входа не было."""
    try:
        return json.loads((PROFILE / "session.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def profile_mount(job_dir: pathlib.Path) -> list[str]:
    """Своя копия профиля на встречу.

    Chromium держит user-data-dir под замком: две параллельные встречи
    с одним каталогом просто не запустились бы. Копия ещё и бережёт
    оригинал — что бы ни записал браузер за встречу, сессия останется той,
    которую подтвердили телефоном.
    """
    if not (PROFILE / "session.json").exists():
        return []
    copy = job_dir / "profile"
    try:
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(PROFILE, copy)
    except Exception as e:  # noqa: BLE001
        log("профиль не скопировался, иду гостем:", f"{type(e).__name__}: {e}")
        return []
    return ["-v", f"{copy}:/profile"]


# ---------------------------------------------------------------- работа

class Job:
    """Одна встреча: запись, расшифровка, бриф."""

    def __init__(self, url: str, chat: int, max_min: int, requester: str = ""):
        self.url = url
        self.chat = chat
        # Тема форума, в которой попросили записать. Запоминаем при создании:
        # бриф приходит через час, и к тому времени последней темой чата
        # может оказаться совсем другая ветка.
        self.thread = THREADS.get(chat)
        self.max_min = max_min
        self.requester = requester
        self.started = time.time()
        self.id = f"{int(self.started)}-{os.urandom(2).hex()}"
        self.dir = JOBS / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.id.split('-')[1]}"
        self.container = f"ai-brief-rec-{self.id}"
        self.stage = "запуск"

    @property
    def minutes(self) -> int:
        return int((time.time() - self.started) / 60)


# Кому отдавать готовый бриф, кроме Телеграма. Формат в .env:
#   WEBHOOKS=https://ваш-сервис/hook|секрет, https://другой/hook|секрет2
# Секрет не передаётся по сети: им подписывается тело запроса, а получатель
# считает подпись сам и сравнивает. Так он отличает наш запрос от чужого,
# даже если адрес перехвата кто-то узнает.
def webhooks() -> list[tuple[str, str]]:
    out = []
    for item in os.environ.get("WEBHOOKS", "").split(","):
        item = item.strip()
        if not item:
            continue
        url, _, secret = item.partition("|")
        if url.startswith("https://") or url.startswith("http://"):
            out.append((url, secret))
        else:
            log(f"WEBHOOKS: пропускаю {item[:40]!r} — нужен полный адрес")
    return out


def deliver_brief(job_dir: pathlib.Path, title: str) -> None:
    """Отдаём бриф внешним получателям.

    Шлём структуру, а не текст для Телеграма: чужому агенту нужны поля,
    а не разметка для чата. Текстовую версию кладём рядом — иногда
    её достаточно, чтобы переслать человеку.
    """
    targets = webhooks()
    if not targets:
        return []
    try:
        brief = json.loads((job_dir / "brief.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"webhook: брифа нет, нечего отдавать: {type(e).__name__}: {e}")
        return []

    meta = job_meta(job_dir)
    transcript = job_dir / "transcript.txt"
    payload = {
        "meeting_id": job_dir.name,
        "title": title,
        "started": meta.get("started"),
        "started_ts": meta.get("start_ts"),
        "duration_sec": meta.get("minutes", 0) * 60,
        "url": meta.get("url"),
        "requester": meta.get("requester"),
        "speakers": sorted({s.get("speaker") for s in
                            json.loads((job_dir / "segments.json").read_text(
                                encoding="utf-8"))}) if
        (job_dir / "segments.json").exists() else [],
        "brief": brief,
        "brief_text": (job_dir / "brief.txt").read_text(encoding="utf-8")
        if (job_dir / "brief.txt").exists() else "",
        "transcript": transcript.read_text(encoding="utf-8")
        if transcript.exists() else "",
    }
    body = json.dumps(payload, ensure_ascii=False).encode()

    results = []
    for url, secret in targets:
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "X-AI-Brief-Event": "brief.ready",
                   "X-AI-Brief-Meeting": job_dir.name}
        if secret:
            headers["X-AI-Brief-Signature"] = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256).hexdigest()
        # Три попытки: у получателя может идти выкладка, а бриф собирается
        # раз в час — терять его из-за минутной недоступности глупо.
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    log(f"webhook {url[:40]}: {resp.status}")
                results.append((url, True, str(resp.status)))
                break
            except Exception as e:  # noqa: BLE001
                log(f"webhook {url[:40]} попытка {attempt}: "
                    f"{type(e).__name__}: {str(e)[:120]}")
                if attempt == 3:
                    results.append((url, False, f"{type(e).__name__}: {str(e)[:80]}"))
                else:
                    time.sleep(5 * attempt)
    return results


def transcribe_job(job_dir: pathlib.Path, chat: int, title: str,
                   token: str | None) -> bool:
    """Расшифровка и бриф по уже записанному аудио. Вынесено отдельно,
    чтобы этот шаг можно было доиграть после перезапуска сервиса."""
    inner = f"/jobs/{job_dir.name}"
    # Отметка на диске, а не в памяти: доигровка раньше гадала по времени
    # файла и 2026-09-02 запустила вторую расшифровку той же встречи поверх
    # идущей — лишние деньги и гонка за один и тот же каталог.
    marker = job_dir / "PROCESSING"
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    try:
        return _transcribe_job(job_dir, chat, title, token, inner)
    finally:
        marker.unlink(missing_ok=True)


CHATS_FILE = ROOT / "chats.json"


def load_chats() -> dict:
    """Чаты, привязанные через Телеграм.

    Лежат отдельно от .env: добавление группы не должно требовать правки
    файла настроек на сервере и перезапуска сервиса. Владельцы по-прежнему
    задаются в .env — их список меняется редко и в обход кода.
    """
    try:
        return json.loads(CHATS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_chats(chats: dict) -> None:
    CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def ago_ru(sec: float) -> str:
    """«2 мин», «3 ч», «4 дн» — короткая давность для отчётов."""
    if sec < 90:
        return f"{int(sec)} с"
    if sec < 5400:
        return f"{int(sec / 60)} мин"
    if sec < 172800:
        return f"{int(sec / 3600)} ч"
    return f"{int(sec / 86400)} дн"


# Очередь расшифровки живёт теперь в gpu-hub — общем шлюзе для машин
# с видеокартами. Своя очередь на диске и SSH-шлюз на четыре команды
# отправлены в историю: та схема была привязана к AI-Brief именем
# каталога и не позволяла подключить второй проект.
# Функциями, а не константами: .env читается в main(), уже после того
# как модуль загружен. Константа взяла бы пустую строку и осталась пустой
# навсегда — /status честно писал «gpu-hub не настроен» при заполненном
# файле настроек.
OPENAI_URL = "https://api.openai.com/v1"


def llm_url() -> str:
    """Куда идёт запрос брифа: любой OpenAI-совместимый сервер."""
    return (os.environ.get("LLM_BASE_URL") or OPENAI_URL).rstrip("/")


def llm_key() -> str:
    if os.environ.get("LLM_API_KEY"):
        return os.environ["LLM_API_KEY"]
    return os.environ.get("OPENAI_API_KEY", "") if llm_url() == OPENAI_URL else ""


def llm_probe() -> tuple[int, str]:
    """Пробный запрос в один токен к модели брифа. Список моделей отдаётся
    и при нулевом балансе, поэтому проверяем настоящим вызовом."""
    model = os.environ.get("LLM_MODEL") or "gpt-5-mini"
    body = {"model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "1"}]}
    if llm_url() == OPENAI_URL:
        # У OpenAI новые модели не принимают max_tokens, а пробу дешевле
        # делать самой младшей моделью.
        body = {"model": "gpt-5-nano", "max_completion_tokens": 1,
                "messages": body["messages"]}
    req = urllib.request.Request(
        f"{llm_url()}/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {llm_key()}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace")
    except (OSError, ValueError) as e:
        return 0, str(e)


def hub_url() -> str:
    return os.environ.get("GPUHUB_URL", "").rstrip("/")


def hub_key() -> str:
    return os.environ.get("GPUHUB_KEY", "")
# Сколько ждать внешнюю расшифровку, прежде чем считать самим.
# у него занимает ~7 минут; 25 — с запасом на очередь и перезагрузку.
def local_wait_min() -> int:
    """Сколько ждать машину с видеокартой, прежде чем платить OpenAI.

    Функцией, а не константой: .env читается при запуске main(), уже после
    загрузки модуля, и константа всегда брала бы значение по умолчанию.
    это выяснилось в неподходящий момент — поднял ожидание до суток,
    чтобы встреча дождалась выключенного ПК, а бот всё равно отсчитал 25
    минут и пошёл считать платно.
    """
    try:
        return max(1, int(os.environ.get("LOCAL_WAIT_MIN", "25")))
    except ValueError:
        return 25


def gpuhub(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{hub_url()}{path}", data=data,
        headers={"Authorization": f"Bearer {hub_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def gpuhub_upload(path: pathlib.Path, timeout: int = 900) -> str:
    """Заливаем файл через curl, а не своими руками.

    Аудио встречи весит сотни мегабайт, а боту отведено 200 МБ памяти:
    собрать такое тело запроса в оперативной памяти — значит убить сервис
    ровно в тот момент, когда он нужен. curl читает файл потоком с диска.
    """
    out = subprocess.run(
        ["curl", "-sS", "--fail", "-m", str(timeout), "-X", "POST",
         f"{hub_url()}/v1/files",
         "-H", f"Authorization: Bearer {hub_key()}",
         "-F", f"file=@{path}"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"загрузка не прошла: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)["id"]


def pack_voices(dst: pathlib.Path) -> pathlib.Path | None:
    """Банк голосов архивом — он едет вместе с задачей.

    Раньше машина забирала его сама отдельной командой шлюза. Это и была
    привязка, из-за которой второй проект получил бы доступ к образцам
    чужих голосов: кто держит ключ, тот берёт банк целиком.
    """
    if not VOICES.is_dir():
        return None
    out = subprocess.run(["tar", "-czf", str(dst), "-C", str(VOICES.parent),
                          VOICES.name], capture_output=True, text=True)
    if out.returncode != 0 or not dst.exists():
        log("банк голосов не упаковался:", out.stderr.strip()[:200])
        return None
    return dst


def wait_for_local(job_dir: pathlib.Path, chat: int, token: str | None) -> bool:
    """Отдаём запись в gpu-hub и ждём расшифровку на машине с видеокартой.

    Это основной путь: точнее на русском, узнаёт голоса, бесплатен. Но
    машину могут выключить, поэтому, если есть ключ OpenAI, через
    LOCAL_WAIT_MIN считаем через него. Без ключа резерва нет — ждём машину
    сколько потребуется: встреча не должна пропадать.
    """
    if os.environ.get("STT_BACKEND", "openai") != "local-first":
        return False
    if not hub_url() or not hub_key():
        log("gpu-hub не настроен (GPUHUB_URL, GPUHUB_KEY) — считаю через OpenAI")
        return False

    result = job_dir / "segments.local.json"
    result.unlink(missing_ok=True)
    try:
        audio_id = gpuhub_upload(job_dir / "audio.wav")
        payload = {"file_id": audio_id}
        archive = pack_voices(job_dir / "voices.tar.gz")
        if archive:
            payload["voices_id"] = gpuhub_upload(archive)
            archive.unlink(missing_ok=True)
        # Приоритет ниже числом — выше в очереди: встреча ждёт человека,
        # а пакетные задачи других проектов подождут её.
        job = gpuhub("/v1/jobs", {"skill": "transcribe.meeting",
                                  "input": payload, "priority": 10})
    except Exception as e:  # noqa: BLE001
        log(f"{job_dir.name}: gpu-hub не принял задачу: {type(e).__name__}: {e}")
        return False

    reserve = bool(os.environ.get("OPENAI_API_KEY"))
    log(f"{job_dir.name}: отдал в gpu-hub ({job['id']}), жду "
        + (f"до {local_wait_min()} мин" if reserve else "без срока: резерва нет"))
    (job_dir / "gpuhub.json").write_text(json.dumps(job), encoding="utf-8")

    deadline = time.time() + (local_wait_min() * 60 if reserve else float("inf"))
    while time.time() < deadline:
        time.sleep(15)
        try:
            state = gpuhub(f"/v1/jobs/{job['id']}")
        except Exception as e:  # noqa: BLE001 — сеть рвётся, это не отказ
            log(f"{job_dir.name}: gpu-hub не ответил: {type(e).__name__}: {e}")
            continue
        if state["status"] == "done":
            segments = (state.get("result") or {}).get("segments")
            if segments is None:
                log(f"{job_dir.name}: в ответе нет сегментов, считаю сам")
                break
            result.write_text(json.dumps(segments, ensure_ascii=False),
                              encoding="utf-8")
            named = (state.get("result") or {}).get("named") or []
            log(f"{job_dir.name}: расшифровка готова, узнаны по голосу: "
                + (", ".join(named) or "никто"))
            return True
        if state["status"] in ("failed", "canceled"):
            log(f"{job_dir.name}: задача в gpu-hub {state['status']}: "
                f"{state.get('error')}")
            break

    try:
        gpuhub(f"/v1/jobs/{job['id']}", None)  # статус уже не важен, снимаем
    except Exception:  # noqa: BLE001
        pass
    log(f"{job_dir.name}: ответа нет за {local_wait_min()} мин, считаю сам")
    if token:
        send(chat, "Машина с видеокартой не ответила — расшифровываю через OpenAI.", token)
    return False


def _transcribe_job(job_dir: pathlib.Path, chat: int, title: str,
                    token: str | None, inner: str) -> bool:
    if wait_for_local(job_dir, chat, token):
        source, extra = f"{inner}/segments.local.json", ["--from-segments"]
    else:
        source, extra = f"{inner}/audio.wav", []

    pipe = subprocess.run(
        ["docker", "run", "--rm", "--memory=900m", "--cpus=1.5",
         "--env-file", str(ROOT / ".env"),
         "-e", f"TZ={os.environ.get('TZ', 'Europe/Moscow')}",
         "-v", f"{ROOT}/jobs:/jobs", "-v", f"{ROOT}/poc:/poc",
         "-v", f"{VOICES}:/voices",
         RECORDER_IMAGE, "python3", "/poc/pipeline/brief.py",
         source, *extra, "--no-send", "--save", inner, "--title", title],
        capture_output=True, text=True)
    (job_dir / "pipeline.log").write_text(pipe.stdout + pipe.stderr, encoding="utf-8")

    brief_txt = job_dir / "brief.txt"
    if not brief_txt.exists():
        tail = (pipe.stdout + pipe.stderr).strip().splitlines()[-3:]
        log("бриф не собрался:", " | ".join(tail))
        if token:
            send(chat, "Запись есть, но бриф не собрался.\n<code>"
                 + "\n".join(tail) + "</code>", token)
        return False

    # Сначала людям в Телеграм, потом машинам: чужой недоступный сервис
    # не должен задерживать бриф живому человеку.
    if token:
        send(chat, brief_txt.read_text(encoding="utf-8"), token)
        transcript = job_dir / "transcript.txt"
        if transcript.exists():
            send_document(chat, transcript, "Полная стенограмма", token)
    else:
        log(brief_txt.read_text(encoding="utf-8"))

    threading.Thread(target=deliver_brief, args=(job_dir, title),
                     daemon=True).start()
    return True


JOB_DIR_RE = re.compile(r"^\d{8}-\d{6}(-[0-9a-f]{4})?$")
LAST_TIMECODE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")
# Разрыв между частями одной встречи. Бот роняет соединение или его зовут
# заново — это та же встреча, а не новая. Час спустя по той же ссылке —
# уже другая.
MERGE_GAP_MIN = int(os.environ.get("MERGE_GAP_MIN", "45"))
# Файлы, которые удаляет ветка «аудио» в /delete: тяжёлое и восстановимое. Стенограммы,
# брифы, метаданные и логи остаются всегда.
DISPOSABLE = ("audio.wav", "audio.speakers.jsonl", "audio.wav.joined.png",
              "audio.wav.testids.json", "audio.wav.stuck.png", "probe.mp3")
# Стенограмма, бриф и разметка речи. usage.json не трогаем: по нему считается
# расход, и стирать историю трат вместе с текстом встречи неправильно.
TEXTS = ("transcript.txt", "brief.txt", "brief.json", "segments.json",
         "segments.local.json", "audio.wav.participants.json")


# Момент перевода сервиса в Europe/Moscow. Каталоги, названные раньше, имеют
# в имени время UTC, названные позже — московское. Разбирать имя без этой
# границы означало бы ошибаться на три часа в одну или другую сторону.
TZ_SWITCH_TS = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc).timestamp()


def dirname_ts(d: pathlib.Path) -> float:
    """Время начала записи из имени каталога.

    Надёжнее mtime: время файла меняется, когда в каталог дописывают бриф,
    и двухчасовая встреча начинала выглядеть начавшейся под вечер.
    """
    try:
        naive = datetime.strptime(d.name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return d.stat().st_mtime
    as_utc = naive.replace(tzinfo=timezone.utc).timestamp()
    if as_utc < TZ_SWITCH_TS:
        return as_utc
    return naive.astimezone().timestamp()


def job_meta(d: pathlib.Path) -> dict:
    """Карточка встречи для истории: заголовок, длительность, есть ли бриф."""
    meta = {}
    if (d / "job.json").exists():
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    audio = d / "audio.wav"
    # wav 16 кГц моно 16 бит — ровно 32000 байт на секунду.
    # Когда аудио удалили через /delete, длительность берём из job.json:
    # иначе двухчасовая встреча схлопывалась в истории до нуля минут.
    if audio.exists():
        secs = int(audio.stat().st_size / 32000)
    else:
        secs = int(meta.get("duration_sec") or 0)
    started = meta.get("started") or datetime.fromtimestamp(
        dirname_ts(d)).strftime("%d.%m %H:%M")
    start_ts = meta.get("started_ts") or dirname_ts(d)
    return {
        "dir": d,
        "started": started,
        "start_ts": start_ts,
        "end_ts": start_ts + secs,
        "minutes": secs // 60,
        "url": meta.get("url", ""),
        "requester": meta.get("requester", ""),
        "chat": meta.get("chat"),
        "title": meta.get("title", f"Встреча {started}"),
        "has_brief": (d / "brief.txt").exists(),
        # Именно наличие файла. Раньше здесь стояло secs > 3, а секунды после
        # удаления берутся из job.json — и удалённые записи навсегда оставались
        # в списке «аудио на диске» с размером 0 МБ.
        "has_audio": audio.exists(),
        "audio_bytes": audio.stat().st_size if audio.exists() else 0,
    }


def transcript_seconds(d: pathlib.Path) -> int:
    """Длительность по последнему таймкоду стенограммы — нужна после того,
    как аудио удалили через /delete, а история должна остаться осмысленной."""
    f = d / "transcript.txt"
    if not f.exists():
        return 0
    for line in reversed(f.read_text(encoding="utf-8").splitlines()):
        m = LAST_TIMECODE.match(line.strip())
        if m:
            h, mi, s = (int(x) for x in m.groups())
            return h * 3600 + mi * 60 + s
    return 0


def sessions() -> list[dict]:
    """Все записи по возрастанию времени. Одна встреча может состоять
    из нескольких — бота роняло и звали заново."""
    if not JOBS.exists():
        return []
    out = []
    for d in sorted(JOBS.iterdir()):
        if not d.is_dir() or not JOB_DIR_RE.match(d.name):
            continue
        it = job_meta(d)
        if not it["has_audio"] and not (d / "transcript.txt").exists():
            continue
        if not it["minutes"]:
            it["minutes"] = transcript_seconds(d) // 60
        out.append(it)
    return sorted(out, key=lambda i: i["start_ts"])


def group_sessions(limit: int = 10) -> list[dict]:
    """Склеиваем подряд идущие записи одной встречи в одну карточку истории."""
    groups: list[dict] = []
    for s in sessions():
        prev = groups[-1] if groups else None
        # Ссылка известна не у всех: записи, сделанные до появления job.json,
        # её не хранят. Для них признаком одной встречи остаётся только
        # близость по времени — этого достаточно, потому что подряд идущие
        # записи по определению относятся к одному разговору.
        same_link = prev and (prev["url"] == s["url"])
        gap_ok = prev and s["start_ts"] - prev["end_ts"] <= MERGE_GAP_MIN * 60
        if same_link and gap_ok:
            prev["parts"].append(s)
            prev["end_ts"] = max(prev["end_ts"], s["end_ts"])
            prev["minutes"] += s["minutes"]
        else:
            groups.append({
                "key": s["dir"].name,
                "url": s["url"],
                "started": s["started"],
                "start_ts": s["start_ts"],
                "end_ts": s["end_ts"],
                "minutes": s["minutes"],
                "requester": s["requester"],
                "title": s["title"],
                "parts": [s],
            })
    return list(reversed(groups))[:limit]


def group_by_key(key: str) -> dict | None:
    for g in group_sessions(limit=200):
        if g["key"] == key:
            return g
    return None


GROUPS = JOBS / "_groups"


def shift_transcript(text: str, delta: int) -> str:
    """Сдвигаем таймкоды части в таймлайн всей встречи."""
    out = []
    for line in text.splitlines():
        m = LAST_TIMECODE.match(line.strip())
        if not m:
            out.append(line)
            continue
        h, mi, s = (int(x) for x in m.groups())
        t = h * 3600 + mi * 60 + s + delta
        out.append(f"[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}]"
                   + line.strip()[10:])
    return "\n".join(out)


def combined_transcript(group: dict) -> pathlib.Path | None:
    """Одна стенограмма на всю встречу, из всех её частей.

    Метки спикеров в каждой части свои (диаризация не знает о соседних
    кусках), поэтому части разделены заголовком — иначе «A» из первой
    части читался бы как тот же человек, что «A» из второй.
    """
    parts = [p for p in group["parts"] if (p["dir"] / "transcript.txt").exists()]
    if not parts:
        return None
    GROUPS.mkdir(parents=True, exist_ok=True)
    dst = GROUPS / f"{group['key']}-transcript.txt"
    if len(parts) == 1:
        return parts[0]["dir"] / "transcript.txt"

    blocks = []
    for n, p in enumerate(parts, 1):
        delta = int(p["start_ts"] - group["start_ts"])
        body = shift_transcript(
            (p["dir"] / "transcript.txt").read_text(encoding="utf-8"), delta)
        blocks.append(f"--- часть {n} из {len(parts)}, начало в {p['started']} "
                      f"(метки спикеров в каждой части свои) ---\n{body}")
    dst.write_text("\n\n".join(blocks), encoding="utf-8")
    return dst


def combined_brief(group: dict) -> pathlib.Path | None:
    """Общий бриф по всей встрече. Для одной части — её собственный;
    для нескольких — один бриф по склеенной стенограмме, а не склейка
    отдельных брифов (иначе решения и задачи дублируются по частям)."""
    parts = group["parts"]
    if len(parts) == 1:
        b = parts[0]["dir"] / "brief.txt"
        return b if b.exists() else None

    GROUPS.mkdir(parents=True, exist_ok=True)
    dst = GROUPS / f"{group['key']}-brief.txt"
    if dst.exists():
        return dst
    tr = combined_transcript(group)
    if not tr:
        return None
    title = f"{group['title']} · {len(parts)} части, {group['minutes']} мин"
    r = subprocess.run(
        ["docker", "run", "--rm", "--memory=900m", "--cpus=1.5",
         "--env-file", str(ROOT / ".env"),
         "-e", f"TZ={os.environ.get('TZ', 'Europe/Moscow')}",
         "-v", f"{ROOT}/jobs:/jobs", "-v", f"{ROOT}/poc:/poc",
         "-v", f"{VOICES}:/voices",
         RECORDER_IMAGE, "python3", "/poc/pipeline/brief.py",
         f"/jobs/_groups/{tr.name}", "--from-transcript", "--no-send",
         "--save", f"/jobs/_groups/{group['key']}", "--title", title],
        capture_output=True, text=True)
    made = GROUPS / group["key"] / "brief.txt"
    if made.exists():
        dst.write_text(made.read_text(encoding="utf-8"), encoding="utf-8")
        return dst
    log("общий бриф не собрался:", (r.stdout + r.stderr).strip()[-200:])
    return None


def recover_jobs(token: str) -> None:
    """Доигрываем встречи, записанные до падения сервиса.

    2026-09-01 сетевой сбой убил процесс посреди двухчасовой встречи:
    контейнер-рекордер дописал аудио до конца, а поток, который должен был
    запустить расшифровку, умер вместе с сервисом — встреча осталась без
    брифа. Теперь запись на диске сама по себе достаточна, чтобы довести
    задачу до конца после рестарта.
    """
    if not JOBS.exists():
        return
    for d in sorted(JOBS.iterdir()):
        audio, brief = d / "audio.wav", d / "brief.txt"
        if not audio.exists() or brief.exists() or audio.stat().st_size < 100_000:
            continue
        # Запись может идти прямо сейчас: контейнер-рекордер переживает
        # перезапуск сервиса. Дописываемый файл трогать нельзя, иначе
        # расшифруем половину встречи и посчитаем её законченной.
        if time.time() - audio.stat().st_mtime < 180:
            log(f"{d.name}: аудио ещё пишется, откладываю")
            continue
        # Расшифровка уже идёт — не начинаем вторую. Отметку старше двух часов
        # считаем брошенной: столько не живёт даже расшифровка четырёхчасовой
        # встречи, а вечная отметка навсегда заблокировала бы доигровку.
        proc = d / "PROCESSING"
        if proc.exists():
            if time.time() - proc.stat().st_mtime < 2 * 3600:
                log(f"{d.name}: расшифровка уже идёт, не дублирую")
                continue
            log(f"{d.name}: брошенная отметка обработки, снимаю")
            proc.unlink(missing_ok=True)
        meta = {}
        if (d / "job.json").exists():
            try:
                meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        # Метка попытки: без неё цикл восстановления каждые 10 минут заново
        # дёргал бы одну и ту же нерасшифровываемую запись и спамил в чат.
        marker = d / "recover.attempted"
        if marker.exists():
            continue
        chat = meta.get("chat")
        if not chat:
            log(f"незавершённая запись {d.name}: неизвестен чат, пропускаю")
            marker.write_text("no-chat", encoding="utf-8")
            continue
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        mins = int(audio.stat().st_size / 32000 / 60)
        log(f"доигрываю незавершённую запись {d.name} ({mins} мин)")
        send(chat, f"Нашёл незавершённую запись от {meta.get('started', d.name)} "
                   f"({mins} мин) — сервис перезапускался. Досчитываю бриф.", token)
        transcribe_job(d, chat, meta.get("title", f"Встреча {d.name}"), token)


def preflight() -> str | None:
    """Чего не хватает до встречи — человеческим языком, или None, если всё есть.

    Без проверки каждая из этих поломок выглядела как трассировка в логе
    и тишина в чате: нет docker — FileNotFoundError в потоке задачи, нет
    образа — «Не получилось записать» с хвостом вывода docker, нет ключа
    OpenAI — встреча записана целиком, а бриф не приходит.
    """
    if not shutil.which("docker"):
        return ("На сервере не найден <code>docker</code>. "
                "Запустите <code>./install.sh</code> заново — он его поставит.")
    try:
        img = subprocess.run(
            ["docker", "image", "inspect", RECORDER_IMAGE, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return ("Не удаётся обратиться к docker: "
                f"<code>{html.escape(str(e))[:150]}</code>. "
                "Проверьте <code>systemctl status docker</code>.")
    if img.returncode != 0:
        return (f"Нет образа записи <code>{RECORDER_IMAGE}</code>. Соберите: "
                f"<code>docker build -t {RECORDER_IMAGE} "
                f"{html.escape(str(ROOT))}/poc/recorder</code>")
    env = html.escape(str(ROOT / ".env"))
    if not llm_key():
        return ("Не задан ключ модели для брифа (<code>LLM_API_KEY</code> "
                f"или <code>OPENAI_API_KEY</code>) в <code>{env}</code>: встречу "
                "я бы записал, а бриф сделать не смогу. Впишите ключ и "
                "перезапустите службу: <code>systemctl restart ai-brief-bot</code>.")
    if not os.environ.get("OPENAI_API_KEY") and not (hub_url() and hub_key()):
        return ("Нечем расшифровывать: нет ни gpu-hub (<code>GPUHUB_URL</code>, "
                "<code>GPUHUB_KEY</code>), ни <code>OPENAI_API_KEY</code> в "
                f"<code>{env}</code>.")
    return None


def run_job(job: Job, token: str | None) -> None:
    job.dir.mkdir(parents=True, exist_ok=True)
    inner = f"/jobs/{job.dir.name}"
    title = f"Встреча в Телемосте, {time.strftime('%d.%m %H:%M')}"
    # Метаданные на диск сразу: после падения сервиса это единственный способ
    # узнать, в какой чат отправлять досчитанный бриф.
    (job.dir / "job.json").write_text(json.dumps(
        {"url": job.url, "chat": job.chat, "requester": job.requester,
         "title": title, "started": time.strftime("%d.%m %H:%M"),
         "started_ts": job.started},
        ensure_ascii=False), encoding="utf-8")

    def say(text: str) -> None:
        remember_thread(job.chat, job.thread)
        log(text)
        if token:
            send(job.chat, text, token)

    problem = preflight()
    if problem:
        job.stage = "ошибка настройки"
        say(problem)
        return

    job.stage = "на встрече"
    who = f" по просьбе {job.requester}" if job.requester else ""
    say(f"Иду на встречу{who} — участники увидят меня как «{html.escape(BOT_NAME)}». "
        "Напишу, когда будет бриф; <code>/stop</code> закончит раньше.")

    # Пустой список — сессии нет, идём гостем: так было до появления аккаунта.
    pmount = profile_mount(job.dir)
    rec = subprocess.run(
        ["docker", "run", "--rm", "--name", job.container,
         # 576 МиБ было замерено на встрече вдвоём; на двадцати
         # участниках браузер декодирует куда больше потоков,
         # а OOM-kill посреди встречи — потеря записи.
         "--memory=2000m", "--cpus=1.5",
         "-e", f"TZ={os.environ.get('TZ', 'Europe/Moscow')}",
         "-v", f"{ROOT}/jobs:/jobs", "-v", f"{ROOT}/poc:/poc",
         *pmount,
         RECORDER_IMAGE, "python3", "/poc/recorder/join_telemost.py",
         "--url", job.url, "--out", f"{inner}/audio.wav",
         "--name", BOT_NAME, "--max-min", str(job.max_min), "--leave-alone-sec", "90",
         *(["--profile", "/profile"] if pmount else [])],
        capture_output=True, text=True)
    # Вывод рекордера — единственный след того, почему он ушёл со встречи.
    (job.dir / "recorder.log").write_text(rec.stdout + rec.stderr, encoding="utf-8")

    audio = job.dir / "audio.wav"
    if not audio.exists() or audio.stat().st_size < 100_000:
        tail = (rec.stdout + rec.stderr).strip().splitlines()[-3:]
        say("Не получилось записать встречу.\n<code>" + "\n".join(tail) + "</code>")
        return

    job.stage = "расшифровка"
    say(f"Записал {job.minutes} мин, расшифровываю.")
    remember_thread(job.chat, job.thread)
    transcribe_job(job.dir, job.chat, title, token)
    job.stage = "готово"



# ---------------------------------------------------------------- голоса

# Имя файла — это имя человека, которое уйдёт в known_speaker_names.
# Разрешаем только безопасные символы: имя приходит из чата.
SAFE_NAME = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 _.-]{2,40}$")
VOICE_SAMPLE_SEC = 8
# Ниже этого образец бесполезен: на 2,45 с «USB здесь» узнавать нечего.
MIN_VOICE_SEC = 4.0
# Предел API на количество known_speaker_references. Дублируется здесь
# намеренно: бот не импортирует конвейер, тот живёт в отдельном контейнере.
MAX_KNOWN_SPEAKERS = 4


def voice_bank() -> list[str]:
    """Имена в банке. Каталог — человек с несколькими образцами, файл —
    старая одиночная запись; служебные каталоги с подчёркиванием пропускаем."""
    if not VOICES.is_dir():
        return []
    names = {f.stem for f in VOICES.glob("*.wav")}
    names |= {d.name for d in VOICES.iterdir()
              if d.is_dir() and not d.name.startswith(("_", "."))
              and any(d.glob("*.wav"))}
    return sorted(names)


def speaker_labels(job_dir: pathlib.Path) -> dict[str, dict]:
    """Метки спикеров встречи с самой длинной их репликой — по ней человек
    узнает себя, и из неё же режется образец голоса."""
    # Берём тот файл, где у реплик есть длительность. В расшифровке OpenAI
    # у встречи 02.09 start и end совпадают — только метка времени, без
    # границ, и любой «кусок речи» выходил нулевым. Локальная разметка
    # pyannote даёт настоящие границы, поэтому она в приоритете.
    segs = []
    for fname in ("segments.local.json", "segments.json"):
        f = job_dir / fname
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        speech = sum(float(x.get("end", 0)) - float(x.get("start", 0))
                     for x in data)
        if speech > 0:
            segs = data
            break
    if not segs:
        return {}
    # Не одна реплика, а самый длинный непрерывный кусок речи человека:
    # отдельная фраза бывает в две секунды («USB здесь»), а такому образцу
    # узнавать голос не по чему. Подряд идущие сегменты одного спикера
    # склеиваем, пока между ними не больше секунды паузы.
    best: dict[str, dict] = {}
    run_spk, run_start, run_end, run_text = None, 0.0, 0.0, ""
    def flush() -> None:
        if run_spk is None:
            return
        dur = run_end - run_start
        if dur > best.get(run_spk, {}).get("dur", 0):
            best[run_spk] = {"dur": dur, "start": run_start, "text": run_text[:60]}

    for s in segs:
        spk = s.get("speaker") or "?"
        st, en = float(s.get("start", 0)), float(s.get("end", 0))
        if spk == run_spk and st - run_end <= 1.0:
            run_end = max(run_end, en)
            run_text += " " + (s.get("text") or "")
        else:
            flush()
            run_spk, run_start, run_end = spk, st, en
            run_text = s.get("text") or ""
    flush()
    return dict(sorted(best.items(), key=lambda kv: -kv[1]["dur"]))


def named_labels(job_name: str) -> dict[str, str]:
    """Какие метки этой встречи уже названы.

    Образец лежит под именем «<встреча>-<метка>.wav» в каталоге человека —
    по нему и восстанавливаем, кто есть кто, без отдельного справочника.
    Старые одиночные образцы встречу не помнят и сюда не попадают.
    """
    out: dict[str, str] = {}
    if not VOICES.is_dir():
        return out
    for d in VOICES.iterdir():
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        for wav in d.glob(f"{job_name}-*.wav"):
            out[wav.stem[len(job_name) + 1:]] = d.name
    return out


def voice_clip(job_dir: pathlib.Path, info: dict, out: pathlib.Path) -> bool:
    """Кусок речи в ogg/opus — формат голосовых сообщений Телеграма."""
    take = min(VOICE_SAMPLE_SEC, info["dur"])
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT}/jobs:/jobs",
         "-v", f"{out.parent}:/out", RECORDER_IMAGE,
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", str(info["start"]), "-t", str(take),
         "-i", f"/jobs/{job_dir.name}/audio.wav",
         "-ac", "1", "-c:a", "libopus", "-b:a", "32k", f"/out/{out.name}"],
        capture_output=True, text=True, timeout=180)
    return out.exists() and out.stat().st_size > 500


def save_voice(job_dir: pathlib.Path, label: str, name: str) -> str:
    """Вырезаем чистый кусок речи одного спикера и кладём в банк.

    API принимает образцы 2–10 секунд, поэтому берём самую длинную реплику
    и от неё не больше восьми секунд — в короткие обрывки попадает то начало
    фразы, то чужой хвост.
    """
    audio = job_dir / "audio.wav"
    if not audio.exists():
        return "Аудио этой встречи уже удалено — образец взять неоткуда."
    info = speaker_labels(job_dir).get(label)
    if not info:
        return "Не нашёл такого спикера в этой встрече."
    if info["dur"] < MIN_VOICE_SEC:
        return (f"У метки {label} самый длинный кусок речи — всего "
                f"{info['dur']:.1f} с. Для узнавания голоса этого мало, "
                f"нужно хотя бы {MIN_VOICE_SEC:.0f} секунды подряд. "
                "Возьмите его с встречи, где он говорил дольше.")
    # Каталог на человека, а не один файл. Раньше имя было именем файла,
    # и второй образец того же человека затирал первый: разметка нескольких
    # меток или нескольких встреч на одно имя молча теряла всё, кроме
    # последнего куска. Теперь образцы копятся, а узнавание берёт лучший.
    person = VOICES / name
    person.mkdir(parents=True, exist_ok=True)
    dst = person / f"{job_dir.name}-{label}.wav"
    take = min(VOICE_SAMPLE_SEC, info["dur"])
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT}/jobs:/jobs",
         "-v", f"{VOICES}:/voices", RECORDER_IMAGE,
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", str(info["start"]), "-t", str(take),
         "-i", f"/jobs/{job_dir.name}/audio.wav",
         "-ac", "1", "-ar", "16000", f"/voices/{name}/{dst.name}"],
        capture_output=True, text=True)
    if not dst.exists():
        log("образец голоса не вырезался:", (r.stdout + r.stderr)[-200:])
        return "Не получилось вырезать образец голоса."
    total = len(list(person.glob("*.wav"))) + len(list(VOICES.glob(f"{name}.wav")))
    log(f"голос в банке: {name} ({take:.0f} с из {job_dir.name}, метка {label}), "
        f"образцов всего {total}")
    more = ("" if total < 2 else
            f" Теперь образцов у него {total} — узнавание надёжнее, "
            "чем по одному.")
    return (f"Запомнил голос: <b>{name}</b> ({take:.0f} с)." + more
            + " На следующих встречах он будет узнан по имени.")


# ---------------------------------------------------------------- бот

class Bot:
    def __init__(self, token: str, allowed: set[int], owners: set[int],
                 max_concurrent: int):
        self.token = token
        # Чаты из .env и привязанные через Телеграм — в одном множестве.
        self.allowed = allowed | {int(k) for k in load_chats()}
        # Владелец — тот, кто может привязывать новые чаты. Его личный чат
        # остаётся в .env: если бы право выдавалось изнутри Телеграма,
        # первый же посторонний, нашедший бота, назначил бы себя сам.
        self.owners = owners
        self.max_concurrent = max_concurrent
        # Своё имя нужно, чтобы отличать «@бот, запиши вот это» от разговора
        # людей между собой. Упоминание доходит до бота даже при включённом
        # privacy mode — в отличие от обычного текста в группе.
        me = tg("getMe", {}, token).get("result", {})
        self.username = (me.get("username") or "").lower()
        print(f"я @{self.username}")
        self.jobs: dict[str, Job] = {}
        self.pending: dict[str, tuple[str, int, str]] = {}
        # Чат, от которого ждём имя для голоса: (каталог встречи, метка).
        self.pending_name: dict[int, tuple[str, str]] = {}
        self.lock = threading.Lock()

    # --- запуск задачи -------------------------------------------------

    def start_job(self, url: str, chat: int, minutes: int, requester: str) -> str:
        """Возвращает текст ответа. Слоты кончились — честно говорим об этом."""
        # Час записи — это 115 МБ. Кончившийся диск обрывает ffmpeg молча,
        # и встреча пропадает без единого сообщения. Лучше отказать на входе.
        try:
            free_mb = os.statvfs("/")
            free_mb = free_mb.f_bavail * free_mb.f_frsize // 1048576
        except Exception:  # noqa: BLE001
            free_mb = None
        if free_mb is not None:
            need_mb = max(300, minutes * 2)  # ~115 МБ/час плюс запас
            if free_mb < need_mb:
                return (f"Не берусь: на диске {free_mb} МБ, а на {minutes} мин "
                        f"записи нужно около {need_mb} МБ. Освободите место "
                        "через /delete — иначе запись оборвётся на середине.")
            if free_mb < need_mb * 2:
                send(chat, f"⚠️ На диске всего {free_mb} МБ. Записи хватит, "
                     "но запаса нет — после встречи стоит почистить /delete.",
                     self.token)
        with self.lock:
            for job in self.jobs.values():
                if job.url == url:
                    return f"Уже на этой встрече, {job.minutes} мин."
            if len(self.jobs) >= self.max_concurrent:
                busy = "; ".join(f"{j.url.rsplit('/', 1)[-1]} ({j.minutes} мин)"
                                 for j in self.jobs.values())
                return ("Все слуги заняты: "
                        f"{len(self.jobs)} из {self.max_concurrent} записей идут — {busy}.\n\n"
                        "Чтобы эта встреча не пропала, включите на ней облачную запись "
                        "Телемоста: файл ляжет на Диск организатора, и бриф по нему "
                        "можно будет собрать позже.")
            job = Job(url, chat, minutes, requester)
            self.jobs[job.id] = job

        def worker() -> None:
            try:
                run_job(job, self.token)
            except Exception as e:  # noqa: BLE001 — падение задачи не должно ронять бота
                send(chat, f"Задача упала: <code>{e}</code>", self.token)
            finally:
                with self.lock:
                    self.jobs.pop(job.id, None)

        threading.Thread(target=worker, daemon=True).start()
        return ""  # дальше рапортует сам worker

    def handle(self, chat: int, text: str, msg_id: int, requester: str = "",
               user_id: int = 0, title: str = "") -> None:
        # Fail-closed. Пустой ALLOWED_CHATS — это НЕ «пускать всех»: бот умеет
        # ходить на встречи и жечь деньги на расшифровке, поэтому незаполненный
        # список означает «никого». Раньше здесь было `if self.allowed and ...`,
        # и при пустом списке бот обслуживал любого, кто его найдёт.
        first = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""

        # Привязку разбираем до проверки доступа: иначе новую группу
        # невозможно было бы добавить изнутри Телеграма — ровно та задача,
        # ради которой это сделано.
        if first in ("/link", "/unlink"):
            self.link_chat(chat, msg_id, user_id, title, unlink=first == "/unlink")
            return

        if chat not in self.allowed:
            # Текст пишем только для чужого чата — это стадия настройки, надо
            # видеть, что вообще доходит. Для разрешённого чата в лог идёт
            # только chat_id: с отключённым privacy mode бот получает всю
            # переписку участников, и складывать её обрывки на диск незачем.
            log(f"msg from chat_id={chat}: {text[:60]!r}")
            send(chat,
                 "Этот чат не в списке разрешённых, поэтому ничего не делаю.\n"
                 f"chat_id: <code>{chat}</code>\n\n"
                 "Владелец может привязать его прямо здесь командой "
                 "<code>/link</code>.",
                 self.token, msg_id)
            return

        cmd = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""

        if cmd in ("/start", "/help"):
            # В группе рядом бывают другие боты, и голая команда уходит не
            # туда: Telegram доставляет её всем. Подсказываем полную форму.
            tail = ""
            if chat < 0 and self.username:
                tail = ("\n\nВ группе пишите команды с моим именем, например "
                        f"<code>/status@{self.username}</code>: так они не "
                        "достанутся другим ботам.")
            send(chat,
                 "<b>AI-Brief</b>\n\n"
                 "Пришлите ссылку на встречу в Телемосте — приду, запишу и верну бриф:\n"
                 "решения, задачи, идеи, открытые вопросы.\n\n"
                 "<code>/brief &lt;ссылка&gt; [минут]</code> — начать\n"
                 "<code>/stop</code> — закончить запись раньше\n"
                 "<code>/status</code> — жив ли сервис, что пишется, что с сервером\n"
                 "<code>/history</code> — прошлые встречи кнопками: бриф и стенограмма\n"
                 "<code>/delete</code> — удалить аудио с диска или стенограмму с брифом\n"
                 "<code>/voices</code> — послушать голоса встречи и подписать, кто есть кто\n"
                 "<code>/login</code> — войти в аккаунт Яндекса бота (имя и аватар на встречах)\n"
                 "<code>/restart</code> — перезапустить сервис, на крайний случай\n"
                 "<code>/link</code> — привязать этот чат к боту (для владельца)\n"
                 "<code>/unlink</code> — отвязать этот чат\n"
                 "<code>/chats</code> — какие чаты обслуживаются\n"
                 "<code>/push</code> — дослать брифы во внешние сервисы\n\n"
                 "В общем чате проще всего <b>тегнуть меня и приложить ссылку</b> — "
                 "пойду сразу. Просто ссылка без обращения — спрошу кнопкой. "
                 "Без явной просьбы ни на какую встречу не пойду.\n"
                 f"Участники встречи видят меня как «{html.escape(BOT_NAME)}»." + tail,
                 self.token, msg_id)
            return

        if cmd == "/history":
            groups = group_sessions()
            if not groups:
                send(chat, "Записей пока нет.", self.token, msg_id)
                return
            rows = []
            for g in groups:
                parts = f" · {len(g['parts'])} части" if len(g["parts"]) > 1 else ""
                who = f" · {g['requester']}" if g["requester"] else ""
                rows.append([{
                    "text": f"{g['started']} · {g['minutes']} мин{parts}{who}",
                    "callback_data": f"hist:{g['key']}",
                }])
            tg("sendMessage", {
                "chat_id": chat,
                "text": "Последние встречи. Нажмите — пришлю бриф и стенограмму.\n"
                        "Встреча, записанная в несколько заходов, отдаётся одним "
                        "брифом и одним файлом.",
                "reply_to_message_id": msg_id,
                "reply_markup": {"inline_keyboard": rows},
            }, self.token)
            return

        if cmd == "/voices":
            bank = voice_bank()
            head = ("<b>Знакомые голоса:</b> " + ", ".join(bank)
                    if bank else "Банк голосов пуст.")
            if len(bank) >= MAX_KNOWN_SPEAKERS:
                head += (f"\n⚠️ Занято все {MAX_KNOWN_SPEAKERS} мест — это "
                         "предел API, лишние голоса не передаются.")
            # Берём самую длинную часть встречи, а не первую: у встречи 15:35
            # первой частью оказалась трёхминутная проверка связи, и бот
            # предлагал образцы оттуда вместо двухчасового обучения.
            rows = []
            for g in group_sessions(limit=8):
                parts = [p for p in g["parts"]
                         if (p["dir"] / "segments.json").exists()]
                if not parts:
                    continue
                longest = max(parts, key=lambda p: p["minutes"])
                rows.append([{"text": f"{g['started']} · {longest['minutes']} мин",
                              "callback_data": f"vmeet:{longest['dir'].name}"}])
            if not rows:
                send(chat, head + "\n\nПока нет встреч, где можно взять образец: "
                     "нужна расшифрованная запись с аудио на диске.",
                     self.token, msg_id)
                return
            send_kb(chat, head + "\n\nВыберите встречу — покажу её спикеров, "
                    "и вы назовёте, кто есть кто.", rows, self.token, msg_id)
            return

        if cmd == "/delete":
            # Два разных удаления: одно про место на диске, другое про то,
            # что сервис помнит о встрече. Смешивать их в одном списке было
            # нельзя — цена ошибки разная.
            audio_n = sum(1 for x in sessions() if x["has_audio"])
            text_n = sum(1 for x in sessions() if x["has_brief"])
            send_kb(chat, "Что удаляем?" + "\n" + "\n"
                    + f"🎧 <b>Аудио</b> — освобождает место, записей: {audio_n}." + "\n"
                    + "Встреча остаётся в истории: текст и бриф на месте." + "\n" + "\n"
                    + f"📝 <b>Стенограмму и бриф</b> — записей: {text_n}." + "\n"
                    + "Стирает встречу из памяти сервиса. Пока цело аудио, "
                      "их можно собрать заново — но это снова расчёт и деньги."
                    + "\n" + "\n"
                    + "🗑 <b>Встречу целиком</b> — аудио, текст и бриф разом. "
                      "Для тестовых записей, которых не должно быть в истории.",
                    [[{"text": "🎧 Аудиофайлы", "callback_data": "delmenu:a"}],
                     [{"text": "📝 Стенограммы и брифы", "callback_data": "delmenu:t"}],
                     [{"text": "🗑 Встречу целиком", "callback_data": "delmenu:w"}]],
                    self.token, msg_id)
            return


        if cmd == "/restart":
            send(chat, "Перезапускаю сервис. Идущие записи не прервутся — "
                       "контейнеры живут отдельно, а брифы по ним досчитаются "
                       "после старта.", self.token, msg_id)
            log("перезапуск по команде из чата", chat)
            subprocess.Popen(["systemctl", "restart", "ai-brief-bot"])
            return

        if cmd == "/login":
            send(chat, "<b>Сначала переключите аккаунт в приложении Яндекса</b> "
                       "на " + html.escape(YANDEX_ACCOUNT or "аккаунт бота")
                       + ".\nВход по QR подтверждает тот аккаунт, который "
                       "активен в приложении, а не тот, что мы хотим.\n\n"
                       "Сейчас пришлю код.", self.token, msg_id)
            threading.Thread(target=self.yandex_login, args=(chat,),
                             daemon=True).start()
            return

        if cmd == "/status":
            send(chat, self.status_report(), self.token, msg_id)
            return

        if cmd == "/push":
            targets = webhooks()
            if not targets:
                send(chat, "Некуда отправлять: внешние получатели не настроены "
                           "(<code>WEBHOOKS</code> в настройках сервиса).",
                     self.token, msg_id)
                return
            items = [x for x in reversed(sessions()) if x["has_brief"]][:12]
            if not items:
                send(chat, "Готовых брифов нет.", self.token, msg_id)
                return
            rows = [[{"text": f"{x['started']} · {x['minutes']} мин",
                      "callback_data": f"push:{x['dir'].name}"}] for x in items]
            rows.append([{"text": "⏩ Все за 30 дней",
                          "callback_data": "pushall:30"}])
            who = ", ".join(u.split("//")[-1].split("/")[0] for u, _ in targets)
            send_kb(chat, f"Кому: <b>{html.escape(who)}</b>\n"
                          "Выберите встречу — отправлю её бриф и стенограмму.",
                    rows, self.token, msg_id)
            return

        if cmd == "/chats":
            send(chat, self.chats_report(), self.token, msg_id)
            return

        if cmd == "/stop":
            with self.lock:
                mine = [j for j in self.jobs.values() if j.chat == chat]
            if not mine:
                send(chat, "Из этого чата ничего не записывается.", self.token, msg_id)
                return
            for job in mine:
                # Файл-флаг, а не docker stop: рекордер сам закроет ffmpeg и
                # оставит целый wav вместо обрубка с битым заголовком.
                (job.dir / "STOP").write_text("", encoding="utf-8")
            send(chat, f"Останавливаю записей: {len(mine)}. Дальше расшифровка.",
                 self.token, msg_id)
            return

        # Ждём имя для голоса — это ответ, а не новая команда.
        if chat in self.pending_name and not cmd.startswith("/"):
            job_name, label = self.pending_name.pop(chat)
            name = text.strip()
            if not SAFE_NAME.match(name):
                send(chat, "Имя должно быть от 2 до 40 символов, без спецзнаков. "
                     "Попробуйте ещё раз через /voices.", self.token, msg_id)
                return
            send(chat, save_voice(JOBS / job_name, label, name), self.token, msg_id)
            return

        found = TELEMOST_RE.search(text)
        if not found:
            send(chat, "Не вижу ссылку на Телемост. Нужна вида "
                       "<code>https://telemost.yandex.ru/j/…</code>", self.token, msg_id)
            return

        minutes = DEFAULT_MAX_MIN
        tail = re.search(r"\b(\d{1,3})\s*$", text.strip())
        if tail:
            minutes = max(1, min(int(tail.group(1)), 240))

        # Явное обращение — команда или упоминание — идём сразу.
        # Просто ссылка в рабочем чате: спрашиваем кнопкой, потому что ссылку
        # чаще кидают людям, а не боту, и вламываться на встречу, которую
        # никто не просил записывать, нельзя.
        mentioned = bool(self.username) and f"@{self.username}" in text.lower()
        if cmd == "/brief" or mentioned:
            reply = self.start_job(found.group(0), chat, minutes, requester)
            if reply:
                send(chat, reply, self.token, msg_id)
            return

        key = os.urandom(4).hex()
        with self.lock:
            self.pending[key] = (found.group(0), minutes, requester)
        tg("sendMessage", {
            "chat_id": chat,
            "text": "Вижу ссылку на встречу. Записать и прислать сюда бриф?",
            "reply_to_message_id": msg_id,
            "reply_markup": {"inline_keyboard": [[
                {"text": "Записать", "callback_data": f"go:{key}"},
                {"text": "Не надо", "callback_data": f"no:{key}"},
            ]]},
        }, self.token)

    def yandex_login(self, chat: int) -> None:
        """Вход по QR: пароль остаётся у владельца, на сервере — только сессия."""
        PROFILE.mkdir(parents=True, exist_ok=True)
        shot = PROFILE / "login.png"
        shot.unlink(missing_ok=True)
        proc = subprocess.Popen(
            ["docker", "run", "--rm", "--name", "ai-brief-login",
             "--memory=1200m", "--cpus=1.5",
             "-e", f"TZ={os.environ.get('TZ', 'Europe/Moscow')}",
             "-v", f"{PROFILE}:/profile", "-v", f"{ROOT}/poc:/poc",
             RECORDER_IMAGE, "python3", "/poc/recorder/yandex_login.py",
             "--profile", "/profile", "--shot", "/profile/login.png",
             "--wait-sec", "240", "--account", YANDEX_ACCOUNT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        # Ждём саму картинку, а не завершения контейнера: он будет жить ещё
        # четыре минуты, пока владелец сканирует код.
        # Код действует около минуты, поэтому контейнер переснимает экран,
        # а мы досылаем свежий снимок, пока идёт ожидание. Иначе владелец
        # наводит камеру на код, который уже не работает.
        sent, last = 0, 0.0
        while proc.poll() is None and sent < 4:
            if shot.exists() and shot.stat().st_size > 5000                     and shot.stat().st_mtime > last:
                time.sleep(1)  # даём скриншоту дописаться на диск
                last = shot.stat().st_mtime
                sent += 1
                send_photo(chat, shot,
                           "Наведите камеру: приложение Яндекс → значок "
                           "«Умная камера» в поисковой строке → на этот код."
                           + ("" if sent == 1 else " (код обновился)"),
                           self.token)
            time.sleep(2)
        if not sent:
            send(chat, "Не получилось показать QR-код — страница входа "
                       "не открылась.", self.token)

        out = (proc.communicate()[0] or "")[-600:]
        log("вход в Яндекс:", out.replace("\n", " | ")[-400:])
        state = session_state()
        if state.get("ok") and (state.get("right") or not YANDEX_ACCOUNT):
            send(chat, "Вход выполнен: <b>"
                 + html.escape(state.get("account") or "аккаунт")
                 + "</b>.\n"
                   "На встречах бот теперь будет со своим именем "
                   "и аватаркой.", self.token)
        elif state.get("ok"):
            send(chat, "Вошёл, но активным оказался <b>"
                 + html.escape(state.get("account") or "другой аккаунт")
                 + "</b>, а нужен <b>" + html.escape(YANDEX_ACCOUNT)
                 + "</b>.\n"
                   "Переключите аккаунт в приложении Яндекса и повторите "
                   "<code>/login</code>.", self.token)
        else:
            send(chat, "Вход не подтверждён. Запись это не ломает — на встречи "
                       "хожу гостем. Попробуйте <code>/login</code> ещё раз.\n"
                       "<code>" + html.escape(out[-300:]) + "</code>", self.token)

    def push_one(self, chat: int, name: str) -> None:
        """Дослать один бриф внешним получателям."""
        d = JOBS / name
        if not JOB_DIR_RE.match(name) or not (d / "brief.json").exists():
            send(chat, "По этой встрече нет готового брифа.", self.token)
            return
        it = job_meta(d)
        results = deliver_brief(d, it["title"])
        ok = [u for u, good, _ in results if good]
        bad = [(u, why) for u, good, why in results if not good]
        text = f"<b>{html.escape(it['title'])}</b>\n"
        if ok:
            text += f"отправлено получателям: {len(ok)}\n"
        for url, why in bad:
            text += (f"не ушло на {html.escape(url.split('//')[-1][:40])}: "
                     f"<code>{html.escape(why)}</code>\n")
        send(chat, text or "Получателей нет.", self.token)

    def push_many(self, chat: int, days: int) -> None:
        """Дослать всё за последние дни — для первого наполнения чужой базы."""
        edge = time.time() - days * 86400
        items = [x for x in sessions()
                 if x["has_brief"] and x["start_ts"] >= edge]
        if not items:
            send(chat, f"За {days} дней готовых брифов нет.", self.token)
            return
        send(chat, f"Отправляю {len(items)} брифов — напишу, когда закончу.",
             self.token)
        sent, failed = 0, []
        for it in items:
            results = deliver_brief(it["dir"], it["title"])
            if results and all(good for _, good, _ in results):
                sent += 1
            else:
                failed.append(it["started"])
        text = f"Готово: отправлено {sent} из {len(items)}."
        if failed:
            text += "\nНе ушли: " + ", ".join(failed[:10])
        send(chat, text, self.token)

    def link_chat(self, chat: int, msg_id: int, user_id: int, title: str,
                  unlink: bool = False) -> None:
        """Привязать или отвязать чат, не трогая .env и не перезапуская сервис.

        Право даёт личный чат владельца из .env, а не роль в группе:
        администратором новой группы может оказаться кто угодно, а платит
        за расшифровку встреч владелец.
        """
        if user_id not in self.owners:
            log(f"/link из chat_id={chat} от user_id={user_id}: не владелец")
            send(chat, "Привязывать чаты может только владелец бота.",
                 self.token, msg_id)
            return

        chats = load_chats()
        key = str(chat)
        name = title or ("личный чат" if chat > 0 else f"группа {chat}")

        if unlink:
            if key not in chats:
                send(chat, "Этот чат и так не привязан через Телеграм. "
                           "Если он работает — значит записан в .env.",
                     self.token, msg_id)
                return
            chats.pop(key)
            save_chats(chats)
            self.allowed.discard(chat)
            log(f"чат отвязан: {chat} ({name})")
            send(chat, f"Отвязал: <b>{html.escape(name)}</b>. "
                       "Больше здесь ничего не делаю.", self.token, msg_id)
            return

        if chat in self.allowed:
            send(chat, f"Этот чат уже разрешён: <b>{html.escape(name)}</b>.",
                 self.token, msg_id)
            return

        chats[key] = {"title": name, "added": time.strftime("%Y-%m-%d %H:%M"),
                      "by": user_id}
        save_chats(chats)
        self.allowed.add(chat)
        log(f"чат привязан: {chat} ({name}) по команде user_id={user_id}")
        send(chat, f"Готово: <b>{html.escape(name)}</b> теперь обслуживается.\n\n"
                   "Киньте ссылку на встречу в Телемосте или наберите "
                   "<code>/help</code>.", self.token, msg_id)

    def chats_report(self) -> str:
        """Какие чаты обслуживаются и откуда они взялись."""
        linked = load_chats()
        lines = ["<b>Разрешённые чаты</b>"]
        for cid in sorted(self.allowed):
            row = linked.get(str(cid))
            if row:
                lines.append(f"• {html.escape(row['title'])} — "
                             f"<code>{cid}</code>, привязан {row['added']}")
            else:
                kind = "личный" if cid > 0 else "группа"
                lines.append(f"• {kind} <code>{cid}</code> — из .env")
        lines.append("\nПривязать новый: <code>/link</code> в нужном чате. "
                     "Отвязать: <code>/unlink</code>.")
        return "\n".join(lines)

    def status_report(self) -> str:
        """Полная картина: не только «что пишу», но и здоров ли сервер.

        Смысл команды — чтобы можно было убедиться, что бот жив и запись
        идёт, не заходя на VPS. Поэтому здесь и место на диске, и доступность
        API, и последние ошибки, а не только список задач.
        """
        def sh(cmd: str) -> str:
            """Вывод команды, безопасный для HTML-разметки Телеграма.

            Строки логов содержат «<urlopen error ...>», Телеграм видит в этом
            незакрытый тег и отклоняет всё сообщение целиком — /status молча
            переставал отвечать.
            """
            try:
                out = subprocess.run(cmd, shell=True, capture_output=True,
                                     text=True, timeout=15).stdout.strip()
            except Exception:  # noqa: BLE001
                return "?"
            return html.escape(out)

        lines = ["<b>Состояние сервиса</b>"]

        with self.lock:
            jobs = list(self.jobs.values())
        if jobs:
            lines.append(f"\n🔴 Записей идёт: {len(jobs)} из {self.max_concurrent}")
            for j in jobs:
                lines.append(f"• {j.stage}, {j.minutes} мин — {j.url}")
        else:
            lines.append(f"\n⚪ Ничего не записываю, свободно "
                         f"слотов: {self.max_concurrent}")

        # Контейнеры видны, даже если поток задачи погиб при перезапуске —
        # это единственный честный признак «запись всё-таки идёт».
        rec = sh("docker ps --filter name=ai-brief-rec- --format '{{.Names}} {{.Status}}'")
        if rec:
            lines.append(f"\n<b>Контейнеры записи</b>\n<code>{rec}</code>")

        # Образ рекордера пропадает молча: он запускается только на время
        # встречи, поэтому любая чистка докера с удалением неиспользуемого
        # сносит его как ненужный. Выясняется это в худший момент — когда
        # бота позвали на встречу (так и случилось 09.09).
        img = sh(f"docker image inspect {RECORDER_IMAGE} "
                 "--format '{{.Id}}' 2>/dev/null")
        if not img.startswith("sha256"):
            lines.append(f"\n❗ <b>Нет образа записи</b> ({RECORDER_IMAGE}) — "
                         "встречи записать не получится. Пересобрать: "
                         "<code>docker build -t " + RECORDER_IMAGE
                         + f" {html.escape(str(ROOT))}/poc/recorder</code>")

        uptime = sh("systemctl show ai-brief-bot -p ActiveEnterTimestamp --value")
        lines.append(f"\n<b>Сервис</b>\nзапущен: {uptime or '?'}")
        restarts = sh("systemctl show ai-brief-bot -p NRestarts --value")
        if restarts and restarts != "0":
            lines.append(f"перезапусков с загрузки: {restarts}")

        mem = sh("free -m | awk '/^Mem:/{print $7\" МБ свободно из \"$2}'")
        swap = sh("free -m | awk '/^Swap:/{print $3\" МБ занято из \"$2}'")
        disk = sh("df -h / | awk 'NR==2{print $4\" свободно, занято \"$5}'")
        used_pct = sh("df / | awk 'NR==2{print $5}' | tr -d '%'")
        audio = sh(f"du -sh '{JOBS}' 2>/dev/null | cut -f1")
        lines.append(f"\n<b>Сервер</b>\nпамять: {mem}\nswap: {swap}\n"
                     f"диск: {disk}\nзаписи занимают: {audio}")
        # Кончившийся диск останавливает запись молча, поэтому предупреждаем
        # заранее: час встречи — это ещё 115 МБ.
        if used_pct.isdigit() and int(used_pct) >= 85:
            lines.append("⚠️ Диск заканчивается — освободите место через /delete")

        # Проверяем именно то, без чего бриф не соберётся.
        lines.append("\n<b>Внешние сервисы</b>")
        if os.environ.get("OPENAI_API_KEY"):
            code = sh("curl -s -o /dev/null -w '%{http_code}' -m 10 "
                      "-H \"Authorization: Bearer $OPENAI_API_KEY\" "
                      "https://api.openai.com/v1/models")
            lines.append("OpenAI (расшифровка): "
                         + ("ключ принят" if code == "200" else f"ПРОБЛЕМА (код {code})"))
        else:
            lines.append("OpenAI: ключа нет — расшифровка только на gpu-hub, без резерва")
        lines.append("Telegram: отвечаю, значит доступен")

        # Сессия Яндекса протухает молча, и узнать об этом на встрече поздно:
        # без неё бот снова гость со случайной аватаркой.
        st = session_state()
        if not st:
            lines.append("Яндекс: входа не было — на встречах гость "
                         "(<code>/login</code>)")
        elif st.get("ok"):
            age = time.time() - (PROFILE / "session.json").stat().st_mtime
            who = html.escape(st.get("account") or "аккаунт")
            if YANDEX_ACCOUNT and not st.get("right"):
                lines.append(f"⚠️ Яндекс: активен {who}, а нужен "
                             + html.escape(YANDEX_ACCOUNT)
                             + " — участники увидят чужое имя")
            else:
                lines.append(f"Яндекс: вход жив, {who} "
                             f"(проверено {ago_ru(age)} назад)")
        else:
            lines.append("⚠️ Яндекс: сессия умерла — иду на встречи гостем, "
                         "нужен <code>/login</code>")

        model = html.escape(os.environ.get("LLM_MODEL") or "gpt-5-mini")
        host = html.escape(urllib.parse.urlsplit(llm_url()).netloc)
        code, probe = llm_probe()
        if "insufficient_quota" in probe or "credit_balance_exhausted" in probe:
            lines.append(f"\n❗ <b>На счёте {host} нет средств.</b> Записи "
                         "сохраняются, но брифы не соберутся.")
        elif code != 200:
            reason = re.search(r'"message":\s*"([^"]{0,120})', probe)
            lines.append(f"\n⚠️ Модель брифа {model} на {host} отвечает ошибкой "
                         f"(код {code}): <code>"
                         + html.escape(reason.group(1) if reason else probe[:100])
                         + "</code>")
        else:
            lines.append(f"Бриф: {model} на {host}, пробный запрос прошёл")

        # Состояние машин с видеокартами спрашиваем у gpu-hub: он и есть
        # единственный, кто их видит. Машина с картой может стоять за NAT
        # и приходить к хабу сама, поэтому «жива ли она» знает только хаб.
        backend = os.environ.get("STT_BACKEND", "openai")
        lines.append("\n<b>Локальная расшифровка</b> (GigaAM + pyannote)")
        if backend != "local-first":
            lines.append("⚪ выключена: STT_BACKEND="
                         + html.escape(backend) + ", считаем через OpenAI")
        if not hub_url() or not hub_key():
            lines.append("🔴 gpu-hub не настроен (GPUHUB_URL, GPUHUB_KEY)")
        else:
            try:
                hub = gpuhub("/v1/status", timeout=15)
                online = [w for w in hub["workers"] if w["online"]]
                busy = [w for w in online if w["busy"]]
                if not online:
                    lines.append("🔴 ни одной машины на связи — брифы "
                                 "соберутся через OpenAI и платно")
                else:
                    names = ", ".join(html.escape(w["name"]) for w in online)
                    mark = "🟡" if busy else "🟢"
                    lines.append(f"{mark} машин на связи: {len(online)} "
                                 f"({names}), заняты: {len(busy)}")
                queued = hub["jobs"].get("queued", 0)
                running = hub["jobs"].get("leased", 0)
                if queued or running:
                    lines.append(f"задач в очереди: {queued}, считается: {running}")
                failed = hub["jobs"].get("failed", 0)
                if failed:
                    lines.append(f"⚠️ задач упало: {failed}")
            except Exception as e:  # noqa: BLE001
                lines.append("⚠️ gpu-hub не отвечает: <code>"
                             + html.escape(f"{type(e).__name__}: {e}")[:120]
                             + "</code>")
        done = len(list(JOBS.glob("*/segments.local.json")))
        if done:
            lines.append(f"расшифровано локально встреч: {done}")

        # Свой учёт расхода: в кабинете OpenAI траты этого проекта смешаны
        # с другими, и «куда ушли деньги» приходилось выяснять по графику.
        spend: dict[str, float] = {}
        for f in JOBS.glob("*/usage.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for model, v in (data.get("by_model") or {}).items():
                spend[model] = spend.get(model, 0.0) + float(v.get("usd", 0))
        if spend:
            total = sum(spend.values())
            lines.append("\n<b>Расход AI-Brief</b> (по нашим записям): "
                         f"${total:.2f}")
            for model, usd in sorted(spend.items(), key=lambda kv: -kv[1]):
                lines.append(f"• {model}: ${usd:.2f}")

        pending = [s for s in sessions() if s["has_audio"] and not s["has_brief"]]
        if pending:
            lines.append(f"\n⚠️ Записей без брифа: {len(pending)} — "
                         "их можно досчитать через /history")

        # Только за сутки: копившиеся с самого начала трейсбеки пугают, но
        # ничего не говорят о том, здоров ли сервис сейчас.
        today = time.strftime("%Y-%m-%d")
        errs = sh(f"grep '^{today}' /var/log/ai-brief-bot.log | grep -c 'error\\|Error'")
        if errs and errs != "0":
            last = sh(f"grep '^{today}' /var/log/ai-brief-bot.log "
                      "| grep 'error\\|Error' | tail -1 | cut -c1-110")
            lines.append(f"\n<b>Ошибки за сегодня</b>: {errs}\n<code>{last}</code>\n"
                         "Сетевые обрывы бот переживает сам — сервис от них "
                         "больше не падает.")
        else:
            lines.append("\n<b>Ошибок за сегодня нет</b>")

        return "\n".join(lines)

    def send_history_item(self, chat: int, name: str) -> None:
        """Отдать бриф и стенограмму по встрече из истории.

        Имя каталога приходит из callback_data, то есть снаружи, — поэтому
        сверяем его с шаблоном и берём каталог строго внутри JOBS, без
        склейки произвольного пути.
        """
        if not JOB_DIR_RE.match(name):
            send(chat, "Не знаю такой записи.", self.token)
            return
        group = group_by_key(name)
        if not group:
            send(chat, "Эта запись уже удалена.", self.token)
            return

        # Часть без брифа тянет за собой всю встречу: пока она не расшифрована,
        # общий бриф будет неполным. Считаем недостающие и выходим — по
        # готовности пользователь нажмёт кнопку ещё раз.
        missing = [p for p in group["parts"]
                   if not p["has_brief"] and p["has_audio"]]
        if missing:
            send(chat, f"У встречи {group['started']} ещё не расшифрованы части: "
                       f"{len(missing)}. Считаю — это несколько минут, потом "
                       "нажмите кнопку снова.", self.token)
            for p in missing:
                threading.Thread(
                    target=transcribe_job,
                    args=(p["dir"], chat, p["title"], None), daemon=True).start()
            return

        parts = len(group["parts"])
        cached = GROUPS / f"{group['key']}-brief.txt"

        # Сборка общего брифа — это платный вызов модели. Делать его молча
        # при каждом нажатии неправильно: брифы частей уже есть, и человек
        # просил показать встречу, а не потратить деньги. Поэтому отдаём
        # готовое, а общий предлагаем кнопкой.
        if parts > 1 and not cached.exists():
            ready = [p for p in group["parts"] if (p["dir"] / "brief.txt").exists()]
            for p in ready:
                send(chat, (p["dir"] / "brief.txt").read_text(encoding="utf-8"),
                     self.token)
            tr = combined_transcript(group)
            if tr:
                send_document(chat, tr, f"Стенограмма целиком: {group['started']}",
                              self.token)
            send_kb(chat,
                    f"Это брифы по частям ({len(ready)} из {parts}). "
                    "Свести их в один общий — отдельный запрос к модели, "
                    "он стоит денег, поэтому спрашиваю.",
                    [[{"text": "Собрать общий бриф",
                       "callback_data": f"vjoin:{group['key']}"}]], self.token)
            return

        brief = combined_brief(group)
        if not brief:
            # Общий бриф требует вызова модели и может не собраться — например,
            # когда кончились деньги на счёте. Но брифы отдельных частей уже
            # есть, и отдавать вместо них «ничего нет» — прямая ложь.
            ready = [p for p in group["parts"] if (p["dir"] / "brief.txt").exists()]
            if not ready:
                send(chat, "По этой встрече нет ни брифа, ни записи.", self.token)
                return
            send(chat, f"Общий бриф собрать не удалось — скорее всего, "
                       f"недоступна модель. Отдаю брифы по частям "
                       f"({len(ready)} из {parts}).", self.token)
            for p in ready:
                send(chat, (p["dir"] / "brief.txt").read_text(encoding="utf-8"),
                     self.token)
            tr = combined_transcript(group)
            if tr:
                send_document(chat, tr, f"Стенограмма целиком: {group['started']}",
                              self.token)
            return

        send(chat, brief.read_text(encoding="utf-8"), self.token)
        tr = combined_transcript(group)
        if tr:
            send_document(chat, tr, f"Стенограмма: {group['started']}"
                          + (f", {parts} части" if parts > 1 else ""), self.token)

    def show_speakers(self, chat: int, name: str) -> None:
        if not JOB_DIR_RE.match(name) or not (JOBS / name).is_dir():
            send(chat, "Такой встречи уже нет.", self.token)
            return
        d = JOBS / name
        labels = speaker_labels(d)
        if not labels:
            send(chat, "У этой встречи нет разметки спикеров.", self.token)
            return
        # Короткие метки не показываем вовсе: предлагать кнопку, которая
        # заведомо ответит отказом, — издевательство над человеком.
        ok = {l: i for l, i in labels.items() if i["dur"] >= MIN_VOICE_SEC}
        skipped = len(labels) - len(ok)
        if not ok:
            send(chat, f"Ни у одной из {len(labels)} меток нет "
                 f"{MIN_VOICE_SEC:.0f} секунд речи подряд — образец взять "
                 "не из чего. Выберите встречу подлиннее.", self.token)
            return
        tail = (f"\nЕщё {skipped} меток пропущено — там речи меньше "
                f"{MIN_VOICE_SEC:.0f} секунд подряд." if skipped else "")
        known = named_labels(name)
        done = sum(1 for lab in ok if lab in known)
        head = f"Спикеры встречи — {len(ok)} голосов."
        if done:
            head += f" Уже названы: {done}."
        send(chat, head + tail + "\nПрисылаю по кусочку речи каждого. "
             "Послушайте и нажмите «Это…» под знакомым голосом.",
             self.token)

        # Кусок речи вместо цитаты: по тексту угадать человека можно
        # далеко не всегда, а по голосу — сразу.
        clips = pathlib.Path(tempfile.mkdtemp(prefix="voices-"))
        try:
            for n, (lab, info) in enumerate(ok.items()):
                out = clips / f"{n}.ogg"
                # Уже названные помечаем прямо в подписи: иначе человек
                # переслушивает и переназывает одно и то же по кругу.
                mine = known.get(lab)
                button = {"inline_keyboard": [[
                    {"text": (f"Переназвать {lab}" if mine
                              else f"Это… (назвать {lab})"),
                     # В данные кнопки Телеграм пускает 64 байта, а метки
                     # после локальной расшифровки стали именами: «Анастасия
                     # имя в кодировке занимает вдвое больше байт, чем букв, и вместе
                     # с именем встречи кнопка перестала отправляться вовсе
                     # (BUTTON_DATA_INVALID, 28.09). Передаём номер метки
                     # в списке — он всегда короткий.
                     "callback_data": f"vlab:{name}|#{n}"}]]}
                cap = (f"✅ {lab} — {mine}" if mine else f"{lab}")
                cap += (f" · {info['dur']:.0f} с — {info['text'][:60]}")
                if voice_clip(d, info, out):
                    send_voice(chat, out, cap, self.token, button)
                else:
                    # Не вырезалось — кнопка всё равно нужна, иначе
                    # метку нельзя будет назвать вовсе.
                    send_kb(chat, cap, button["inline_keyboard"], self.token)
        finally:
            shutil.rmtree(clips, ignore_errors=True)

    def list_audio(self, chat: int, msg_id: int | None = None) -> None:
        """Аудиофайлы на диске — то, что занимает место."""
        items = [x for x in reversed(sessions()) if x["has_audio"]]
        if not items:
            send(chat, "Аудиофайлов на диске нет.", self.token, msg_id)
            return
        # Подписываем каждый файл встречей из /history: без этого два
        # списка выглядели несвязанными — в одном встречи, в другом файлы.
        belongs = {}
        for g in group_sessions(limit=200):
            for n, part in enumerate(g["parts"], 1):
                belongs[part["dir"].name] = (
                    f" · встреча {g['started']}"
                    + (f", часть {n} из {len(g['parts'])}"
                       if len(g["parts"]) > 1 else ""))
        rows = [[{
            "text": f"{x['minutes']} мин · {x['audio_bytes'] / 1048576:.0f} МБ"
                    + belongs.get(x["dir"].name, f" · {x['started']}"),
            "callback_data": f"del:{x['dir'].name}",
        }] for x in items]
        total = sum(x["audio_bytes"] for x in items) / 1048576
        send_kb(chat, f"🎧 Аудиозаписи на диске — всего {total:.0f} МБ." + "\n"
                + "Это файлы записи: у встречи из нескольких заходов "
                  "их несколько." + "\n" + "Стенограммы и брифы остаются в /history.",
                rows, self.token, msg_id)

    def list_texts(self, chat: int, msg_id: int | None = None) -> None:
        """Стенограммы и брифы — то, что сервис помнит о встрече."""
        items = [x for x in reversed(sessions()) if x["has_brief"]]
        if not items:
            send(chat, "Стенограмм и брифов нет.", self.token, msg_id)
            return
        rows = [[{
            "text": f"{x['started']} · {x['minutes']} мин"
                    + (" · аудио есть" if x["has_audio"] else " · без аудио"),
            "callback_data": f"delt:{x['dir'].name}",
        }] for x in items]
        send_kb(chat, "📝 Стенограммы и брифы." + "\n"
                + "Удаление стирает встречу из истории." + "\n"
                + "Где аудио ещё цело — бриф можно собрать заново, "
                  "но это платный расчёт. Где нет — встреча пропадёт совсем.",
                rows, self.token, msg_id)

    def confirm_delete(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name) or not (JOBS / name).is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        it = job_meta(JOBS / name)
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Удалить аудио встречи {it['started']} "
                    f"({it['minutes']} мин, {it['audio_bytes'] / 1048576:.0f} МБ)?\n\n"
                    "Стенограмма и бриф останутся в истории. "
                    "Само аудио восстановить будет нельзя.",
            "reply_markup": {"inline_keyboard": [[
                {"text": "Удалить", "callback_data": f"delyes:{name}"},
                {"text": "Отмена", "callback_data": "delno:"},
            ]]},
        }, self.token)

    def do_delete(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name):
            return
        d = JOBS / name
        if not d.is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        if not (d / "transcript.txt").exists():
            tg("editMessageText", {
                "chat_id": chat, "message_id": msg_id,
                "text": "Не удаляю: по этой записи ещё нет стенограммы, "
                        "и удалить аудио — значит потерять встречу целиком. "
                        "Сначала соберите бриф через /history."}, self.token)
            return

        # Длительность живёт в размере wav — перед удалением переносим её
        # в метаданные, иначе история потеряет, сколько шла встреча.
        it = job_meta(d)
        meta_file = d / "job.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        meta.setdefault("started", it["started"])
        meta.setdefault("started_ts", it["start_ts"])
        meta["duration_sec"] = it["minutes"] * 60 or transcript_seconds(d)
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        freed = 0
        for fname in DISPOSABLE:
            f = d / fname
            if f.exists():
                freed += f.stat().st_size
                f.unlink()
        for f in d.glob("*.mp3"):  # временные куски, если остались от сбоя
            freed += f.stat().st_size
            f.unlink()
        log(f"удалено аудио {name}, освобождено {freed / 1048576:.0f} МБ")
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Удалено, освободилось {freed / 1048576:.0f} МБ. "
                    "Стенограмма и бриф остались в /history."}, self.token)

    def confirm_delete_text(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name) or not (JOBS / name).is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        d = JOBS / name
        it = job_meta(d)
        # Разная цена ошибки: с целым аудио это трата денег на пересчёт,
        # без аудио — потеря встречи навсегда. Так и пишем.
        if it["has_audio"]:
            warn = ("Аудио останется — бриф можно будет собрать заново "
                    "через /history, это платный расчёт.")
        else:
            warn = ("⚠️ Аудио этой встречи уже удалено. Восстановить будет "
                    "нечем — встреча исчезнет насовсем.")
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Удалить стенограмму и бриф встречи {it['started']} "
                    f"({it['minutes']} мин)?" + chr(10) + chr(10) + warn,
            "reply_markup": {"inline_keyboard": [[
                {"text": "Удалить", "callback_data": f"delyest:{name}"},
                {"text": "Отмена", "callback_data": "delno:"},
            ]]},
        }, self.token)

    def do_delete_text(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name):
            return
        d = JOBS / name
        if not d.is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        gone = 0
        for fname in TEXTS:
            f = d / fname
            if f.exists():
                f.unlink()
                gone += 1
        # Общий бриф встречи собран из этих же частей и теперь врёт —
        # сбрасываем кэш, иначе /history покажет удалённое.
        groups = JOBS / "_groups"
        if groups.is_dir():
            for f in groups.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        log(f"удалены тексты {name}: файлов {gone}")
        tail = ("" if (d / "audio.wav").exists()
                else " Аудио этой встречи тоже нет — каталог остался пустым.")
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Удалено: стенограмма и бриф ({gone} файлов)." + tail},
            self.token)

    def list_whole(self, chat: int, msg_id: int | None = None) -> None:
        """Встречи целиком — обычно так чистят тестовые записи."""
        items = list(reversed(sessions()))
        if not items:
            send(chat, "Встреч в истории нет.", self.token, msg_id)
            return
        rows = [[{
            "text": f"{x['started']} · {x['minutes']} мин"
                    + (f" · {x['audio_bytes'] / 1048576:.0f} МБ"
                       if x["has_audio"] else " · без аудио")
                    + ("" if x["has_brief"] else " · без брифа"),
            "callback_data": f"delw:{x['dir'].name}",
        }] for x in items]
        send_kb(chat, "🗑 Встречи целиком." + "\n"
                + "Удаляется всё: аудио, стенограмма, бриф, служебные файлы." + "\n"
                + "Восстановить нельзя. Встреча исчезнет и из /history.",
                rows, self.token, msg_id)

    def confirm_delete_whole(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name) or not (JOBS / name).is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        it = job_meta(JOBS / name)
        size = sum(f.stat().st_size for f in (JOBS / name).rglob("*")
                   if f.is_file()) / 1048576
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Удалить встречу {it['started']} целиком "
                    f"({it['minutes']} мин, {size:.0f} МБ)?" + chr(10) + chr(10)
                    + "Уйдёт всё: аудио, стенограмма, бриф. "
                      "Восстановить будет нечем.",
            "reply_markup": {"inline_keyboard": [[
                {"text": "Удалить всё", "callback_data": f"delyesw:{name}"},
                {"text": "Отмена", "callback_data": "delno:"},
            ]]},
        }, self.token)

    def do_delete_whole(self, chat: int, msg_id: int, name: str) -> None:
        if not JOB_DIR_RE.match(name):
            return
        d = JOBS / name
        if not d.is_dir():
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Такой записи уже нет."}, self.token)
            return
        # Идущую запись не трогаем: каталог у неё занят рекордером, и снос
        # посреди встречи оставил бы контейнер писать в пустоту.
        with self.lock:
            busy = any(j.dir == d for j in self.jobs.values())
        if busy:
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Эта встреча сейчас записывается — "
                                           "сначала /stop."}, self.token)
            return
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1048576
        shutil.rmtree(d, ignore_errors=True)
        groups = JOBS / "_groups"
        if groups.is_dir():
            for f in groups.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        log(f"удалена встреча целиком {name}, освобождено {size:.0f} МБ")
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": f"Встреча удалена целиком, освободилось {size:.0f} МБ."},
            self.token)

    def handle_callback(self, cb: dict) -> None:
        data = cb.get("data", "")
        chat = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        # Кнопку нажали в какой-то теме — отвечать надо туда же.
        remember_thread(chat, cb["message"].get("message_thread_id"))
        who = cb.get("from", {}).get("first_name", "")
        action, _, key = data.partition(":")

        if action == "vjoin":
            tg("answerCallbackQuery", {"callback_query_id": cb["id"]}, self.token)
            if chat not in self.allowed:
                return

            def build() -> None:
                g = group_by_key(key)
                if not g:
                    send(chat, "Эта встреча уже недоступна.", self.token)
                    return
                send(chat, "Собираю общий бриф, это несколько минут.", self.token)
                b = combined_brief(g)
                if b:
                    send(chat, b.read_text(encoding="utf-8"), self.token)
                else:
                    send(chat, "Общий бриф собрать не удалось — проверьте "
                               "/status, скорее всего дело в счёте OpenAI.",
                         self.token)

            threading.Thread(target=build, daemon=True).start()
            return

        if action in ("vmeet", "vlab"):
            tg("answerCallbackQuery", {"callback_query_id": cb["id"]}, self.token)
            if chat not in self.allowed:
                return
            if action == "vmeet":
                self.show_speakers(chat, key)
            else:
                job_name, _, label = key.partition("|")
                if label.startswith("#"):
                    # Номер метки разворачиваем в саму метку тем же
                    # порядком, каким показывали образцы.
                    labels = speaker_labels(JOBS / job_name)
                    shown = [l for l, i in labels.items()
                             if i["dur"] >= MIN_VOICE_SEC]
                    idx = int(label[1:]) if label[1:].isdigit() else -1
                    label = shown[idx] if 0 <= idx < len(shown) else ""
                if not label:
                    send(chat, "Эта кнопка устарела — откройте /voices заново.",
                         self.token)
                    return
                self.pending_name[chat] = (job_name, label)
                r = tg("sendMessage", {
                    "chat_id": chat,
                    "text": f"Кто говорит под меткой <b>{label}</b>? "
                            "Ответьте одним сообщением — именем.",
                    "parse_mode": "HTML",
                    "reply_markup": {"force_reply": True},
                }, self.token)
                if not r.get("ok"):
                    log("запрос имени не дошёл:", str(r.get("error"))[:200])
                    send(chat, f"Напишите имя для метки {label} одним сообщением.",
                         self.token)
            return

        if action in ("push", "pushall"):
            tg("answerCallbackQuery", {"callback_query_id": cb["id"]}, self.token)
            if chat not in self.allowed:
                return
            # В отдельном потоке: отправка десятка брифов занимает минуты,
            # а опрос Телеграма за это время встанет.
            target = (self.push_many if action == "pushall" else self.push_one)
            arg = int(key) if action == "pushall" else key
            threading.Thread(target=target, args=(chat, arg),
                             daemon=True).start()
            return

        if action in ("hist", "del", "delyes", "delno", "delmenu",
                      "delt", "delyest", "delw", "delyesw"):
            tg("answerCallbackQuery", {"callback_query_id": cb["id"]}, self.token)
            if chat not in self.allowed:
                return
            if action == "hist":
                threading.Thread(target=self.send_history_item,
                                 args=(chat, key), daemon=True).start()
            elif action == "del":
                self.confirm_delete(chat, msg_id, key)
            elif action == "delyes":
                self.do_delete(chat, msg_id, key)
            elif action == "delmenu":
                {"a": self.list_audio, "t": self.list_texts,
                 "w": self.list_whole}[key](chat)
            elif action == "delt":
                self.confirm_delete_text(chat, msg_id, key)
            elif action == "delyest":
                self.do_delete_text(chat, msg_id, key)
            elif action == "delw":
                self.confirm_delete_whole(chat, msg_id, key)
            elif action == "delyesw":
                self.do_delete_whole(chat, msg_id, key)
            else:
                tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                       "text": "Ничего не удалял."}, self.token)
            return

        with self.lock:
            item = self.pending.pop(key, None)
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]}, self.token)

        if chat not in self.allowed:
            return
        if not item:
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": "Эта кнопка уже неактуальна."}, self.token)
            return
        url, minutes, requester = item
        if action == "no":
            tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                                   "text": f"Хорошо, не записываю. ({who})"}, self.token)
            return

        reply = self.start_job(url, chat, minutes, who or requester)
        tg("editMessageText", {
            "chat_id": chat, "message_id": msg_id,
            "text": reply or f"Записываю эту встречу. ({who})",
        }, self.token)

    def poll(self) -> None:
        log("bot started")
        offset = 0
        while True:
            # channel_post обязателен: в канале сообщения приходят этим типом,
            # и без подписки Телеграм их просто отбрасывает — со стороны это
            # неотличимо от «бот сломался».
            r = tg("getUpdates", {"offset": offset, "timeout": 50,
                                  "allowed_updates": ["message", "edited_message",
                                                      "channel_post", "callback_query"]},
                   self.token)
            if not r.get("ok"):
                log("getUpdates:", str(r)[:200])
                if r.get("error_code") == 401:
                    # Неверный токен не лечится повтором: раньше здесь был
                    # цикл с запросом раз в секунду и строкой в логе на каждый.
                    log("Telegram отклонил токен (401). Проверьте "
                        "TELEGRAM_BOT_TOKEN в .env и перезапустите службу.")
                    time.sleep(60)
                    continue
                if "error" in r:
                    log("нет связи с api.telegram.org — проверьте интернет и "
                        "доступность Telegram с этого сервера")
                time.sleep(1)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                # Пока идёт настройка — видно каждый апдейт и его тип, иначе
                # «бот молчит» невозможно отличить от «до бота не доходит».
                log(f"апдейт {[k for k in upd if k != 'update_id']}")
                try:
                    if "callback_query" in upd:
                        # В отдельном потоке: обработчики дёргают docker и
                        # шлют сообщения, а пока это идёт, бот не читал бы
                        # входящие — так и терялся ответ с именем.
                        threading.Thread(target=self.handle_callback,
                                         args=(upd["callback_query"],),
                                         daemon=True).start()
                        continue
                    msg = (upd.get("message") or upd.get("channel_post")
                           or upd.get("edited_message") or {})
                    text = msg.get("text")
                    if not text:
                        log(f"апдейт без текста: {list(upd)[1:]}")
                        continue
                    who = (msg.get("from") or {}).get("first_name", "")
                    remember_thread(msg["chat"]["id"],
                                    msg.get("message_thread_id"))
                    self.handle(msg["chat"]["id"], text, msg["message_id"], who,
                                user_id=(msg.get("from") or {}).get("id", 0),
                                title=(msg["chat"].get("title")
                                       or msg["chat"].get("username") or ""))
                except Exception as e:  # noqa: BLE001
                    # Ошибка обработчика раньше означала полную тишину в чате:
                    # человек видит «бот не работает», причина только в логе.
                    # Так пропала /voices из-за необъявленной константы.
                    log("handle failed:", repr(e))
                    chat_id = ((upd.get("message") or upd.get("channel_post")
                                or {}).get("chat") or {}).get("id")
                    if chat_id in self.allowed:
                        send(chat_id, f"Команда сломалась: <code>"
                             f"{html.escape(str(e))[:200]}</code>\n"
                             "Это баг, а не ваша ошибка — уже видно в логе.",
                             self.token)


# Раз в шесть часов: сессия Яндекса протухает молча, и выяснять это
# на встрече поздно. Проверка стоит одного запуска контейнера.
SESSION_CHECK_SEC = 6 * 3600


def check_session() -> None:
    marker = PROFILE / "session.json"
    if not marker.exists():
        return  # входа не было — проверять нечего
    if time.time() - marker.stat().st_mtime < SESSION_CHECK_SEC:
        return
    was_ok = session_state().get("ok")
    subprocess.run(
        ["docker", "run", "--rm", "--name", "ai-brief-session",
         "--memory=1200m", "--cpus=1.0",
         "-v", f"{PROFILE}:/profile", "-v", f"{ROOT}/poc:/poc",
         RECORDER_IMAGE, "python3", "/poc/recorder/yandex_login.py",
         "--profile", "/profile", "--check", "--account", YANDEX_ACCOUNT],
        capture_output=True, text=True, timeout=300)
    now_ok = session_state().get("ok")
    if was_ok and not now_ok:
        log("сессия Яндекса умерла — встречи снова гостем")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-job", metavar="URL",
                    help="прогнать задачу разово, без телеграма — для проверки")
    ap.add_argument("--max-min", type=int, default=3)
    args = ap.parse_args()

    load_env()
    JOBS.mkdir(parents=True, exist_ok=True)

    if args.run_job:
        run_job(Job(args.run_job, 0, args.max_min), token=None)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(f"TELEGRAM_BOT_TOKEN не задан в {ROOT / '.env'}. "
                 "Впишите токен от @BotFather (см. .env.example) и перезапустите службу.")

    # Мусор в ALLOWED_CHATS не должен ронять сервис: незапустившийся бот
    # не может даже подсказать правильный chat_id, и человек остаётся
    # наедине с циклом перезапусков.
    allowed: set[int] = set()
    for raw in os.environ.get("ALLOWED_CHATS", "").replace(" ", "").split(","):
        if not raw:
            continue
        try:
            allowed.add(int(raw))
        except ValueError:
            log(f"ALLOWED_CHATS: пропускаю нечисловое значение {raw!r}")
    if not allowed:
        log("ALLOWED_CHATS пуст: никого не обслуживаю, каждому отвечу только "
              "его chat_id — впишите нужный в .env и перезапустите")

    # Сколько записей разом тянет бокс — измеряется, а не угадывается,
    # см. README (раздел «Ресурсы»). Меняется без правки кода.
    max_concurrent = max(1, int(os.environ.get("MAX_CONCURRENT") or "1"))

    # Владелец — тот, кто может привязывать новые чаты командой /link.
    # Если OWNER_IDS не задан, считаем владельцами тех, чьи ЛИЧНЫЕ чаты
    # уже разрешены: у личного чата идентификатор совпадает с
    # идентификатором пользователя и всегда положителен, у групп —
    # отрицателен. Это не догадка, а свойство Telegram.
    owners: set[int] = set()
    for raw in os.environ.get("OWNER_IDS", "").replace(" ", "").split(","):
        if raw:
            try:
                owners.add(int(raw))
            except ValueError:
                log(f"OWNER_IDS: пропускаю нечисловое значение {raw!r}")
    if not owners:
        owners = {cid for cid in allowed if cid > 0}
    log("владельцы: " + (", ".join(str(o) for o in sorted(owners)) or "никого"))

    # Служба стартует и без образа записи или ключа OpenAI — иначе человек
    # не смог бы даже написать боту и узнать, что не так. Но в лог — сразу.
    problem = preflight()
    if problem:
        log("ВНИМАНИЕ, встречи пока не заработают:",
            html.unescape(re.sub(r"<[^>]+>", "", problem)))

    bot = Bot(token, allowed, owners, max_concurrent)

    def recovery_loop() -> None:
        # Не только на старте: задачу может осиротить падение потока, а не
        # всего процесса — тогда перезапуска, который её подберёт, не будет.
        while True:
            try:
                recover_jobs(token)
            except Exception as e:  # noqa: BLE001
                log("recover_jobs упал:", e)
            try:
                check_session()
            except Exception as e:  # noqa: BLE001
                log("проверка сессии упала:", e)
            time.sleep(600)

    threading.Thread(target=recovery_loop, daemon=True).start()
    bot.poll()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
