"""Путь B: бот-участник заходит в Телемост по ссылке и пишет звук встречи.

Запускается внутри контейнера ai-brief-recorder (entrypoint.sh уже поднял
Xvfb + PulseAudio с виртуальной картой vsink). Chromium играет звук встречи
в vsink, ffmpeg параллельно пишет vsink.monitor в wav 16 кГц моно.

Селекторы Телемоста намеренно вынесены в SELECTORS и читаются из
--selectors config.json: вёрстка чужого продукта меняется, и это единственное
место, которое придётся править (риск R1 в docs/TZ.md).

Режим --discover не заходит на встречу, а сохраняет скриншот и HTML страницы
входа — им фиксируются реальные селекторы на первой живой встрече.

Использование:
    python3 join_telemost.py --url https://telemost.yandex.ru/j/123 \
        --out /work/meeting.wav --name "Cyber Brief" --max-min 120
    python3 join_telemost.py --url ... --discover --out /work/discover
"""
from __future__ import annotations

import argparse
import array
import json
import pathlib
import subprocess
import time

from playwright.sync_api import sync_playwright

# Селекторы сняты с живого экрана входа 2026-08-31 (см. --discover).
# Телемост проставляет стабильные data-testid — держимся за них, а не за
# классы вида Orb-Button_view_brand, которые меняются от сборки к сборке.
SELECTORS = {
    # Телемост слился с Мессенджером, и разметка сменилась
    # целиком: вместо data-testid у кнопок теперь aria-label, а до звонка
    # выстроилась череда окон. Держимся за aria-label — он живёт дольше
    # классов и обязан быть осмысленным, это требование доступности.
    #
    # Порядок окон: приветствие «Большое обновление» -> «Разрешите
    # находить вас» -> выбор «в браузере или в приложении» ->
    # подтверждение профиля -> сам вход в звонок.
    "onboarding_ok": [
        '[data-testid="telemost-3-onboarding-confirm"]',
        '[data-testid="telemost-3-onboarding-close"]',
        'button:has-text("Звучит отлично")',
    ],
    # Предложение раздать номер телефона тем, у кого он сохранён.
    # Боту это не нужно: отвечаем «Не сейчас».
    "not_now": [
        '[data-testid="independent-onboarding-modal-modal"] button:has-text("Не сейчас")',
        'button:has-text("Не сейчас")',
    ],
    "continue_in_browser": [
        '[data-testid="meeting-continue-in-browser-continue"]',
        'button:has-text("Продолжить в браузере")',
    ],
    "profile_ok": [
        'button:has-text("Всё хорошо")',
        'button:has-text("Все хорошо")',
    ],
    "name_input": [
        '[data-testid="orb-textinput-input"]',
        'input[type="text"]',
    ],
    "join_button": [
        'button:has-text("Подключиться")',
        '[aria-label="Подключиться"]',
        '[data-testid="enter-conference-button"]',
        'button:has-text("Присоединиться")',
    ],
    "mic_toggle": [
        '[aria-label="Выключить микрофон"]',
        '[data-testid="turn-off-mic-button"]',
    ],
    "cam_toggle": [
        '[aria-label="Выключить камеру"]',
        '[aria-label="Выключить видео"]',
        '[data-testid="turn-off-camera-button"]',
    ],
    # Признак того, что мы внутри звонка: кнопка выхода появляется только там.
    "in_call_marker": [
        '[aria-label="Завершить звонок"]',
        '[aria-label="Выйти из звонка"]',
        '[data-testid="end-call-alt-button"]',
    ],
    "participants_button": [
        '[aria-label="Участники"]',
        '[data-testid="participants-button"]',
    ],
    "chat_button": [
        '[aria-label="Чат"]',
        '[data-testid="chat-alt-button"]',
    ],
    "participant_item": [
        '[data-testid="participant-item"]',
        '[class*="participant"]',
    ],
    "speaking_marker": '[class*="speaking"], [data-speaking="true"]',
}

SILENCE_PEAK = 900  # замеры: живая речь — десятки тысяч, пустая комната — сотни
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def first_visible(page, candidates, timeout_ms=15000):
    """Телемост рендерит вход по-разному, поэтому ищем первый живой кандидат."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                pass
        page.wait_for_timeout(300)
    return None


def recent_peak(wav: pathlib.Path, seconds: int) -> int | None:
    """Пиковая амплитуда последних N секунд записи.

    Читаем хвост файла напрямую: wav 16 кГц моно 16 бит — это ровно
    32000 байт в секунду сырого PCM, никакой ffmpeg не нужен. Нужно, чтобы
    поймать конец встречи по тишине: 2026-09-02 организатор закрыл встречу
    для всех, а бот остался в комнате, где счётчик показывал троих, и писал
    ещё два часа. Признаки из интерфейса там бесполезны, а тишина — нет.
    """
    try:
        size = wav.stat().st_size
    except OSError:
        return None
    want = seconds * 32000
    if size < want + 44:
        return None
    with wav.open("rb") as f:
        f.seek(size - want)
        raw = f.read(want)
    samples = array.array("h")
    samples.frombytes(raw[:len(raw) // 2 * 2])
    return max(max(samples), -min(samples)) if samples else 0


def collect_participants(page, out: pathlib.Path, shot=None) -> list[str]:
    """Имена участников из интерфейса Телемоста.

    Диаризация даёт буквы, а имена людей висят прямо на плитках и в панели
    «Участники» — это бесплатный источник, которого нет в аудио. Панель
    открываем и закрываем, попутно сохраняя её разметку: селекторы панели
    ещё не подтверждены живой встречей, и дамп даст их зафиксировать.
    """
    names: list[str] = []
    try:
        for sel in SELECTORS["participants_button"]:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(1500)
                break
        names = page.evaluate("""() => {
            const out = new Set();
            document.querySelectorAll('[data-testid], [class*="participant"], [class*="Participant"]')
                .forEach(el => {
                    const t = (el.innerText || '').trim();
                    // Имя — короткая строка без переводов строк; всё длинное
                    // это контейнеры, всё пустое — обёртки.
                    if (t && t.length < 60 && !t.includes('\\n')) out.add(t);
                });
            return [...out];
        }""")
        (shot or page).screenshot(path=f"{out}.participants.png")
        pathlib.Path(f"{out}.participants.html").write_text(
            page.content()[:400000], encoding="utf-8")
        page.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001 — панель не должна ломать запись
        print(f"участников прочитать не удалось: {type(e).__name__}: {e}", flush=True)

    # В улов вместе с именами попадают подписи кнопок и плашки тарифа —
    # проверено на живой комнате: «Копировать ссылку», «2+1 ТБ». Точную
    # разметку панели зафиксируем по participants.html с многолюдной встречи,
    # а пока отсеиваем очевидное.
    junk = ("копировать", "ссылк", "участник", "чат", "тб", "гб", "демонстрац",
            "войти", "выйти", "настройк", "микрофон", "камер", "подключ",
            "организатор", "запись", "поднят", "рук")

    def looks_like_name(n: str) -> bool:
        if not any(c.isalpha() for c in n):
            return False
        if any(j in n.lower() for j in junk):
            return False
        if n.replace("+", "").replace(" ", "").isdigit():
            return False
        # Монограммы аватарок: «АН», «МА», «ЕК» — две-три заглавные буквы
        # без пробелов. Живое имя короче четырёх символов не бывает.
        letters = [c for c in n if c.isalpha()]
        if len(n) <= 3 and all(c.isupper() for c in letters):
            return False
        return True

    clean = [n for n in names if looks_like_name(n)]

    pathlib.Path(f"{out}.participants.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"участники: {clean}", flush=True)
    return clean


def participant_count(page) -> int | None:
    """Число участников Телемост печатает внутри кнопки «Участники»."""
    for sel in SELECTORS["participants_button"]:
        try:
            text = page.locator(sel).first.inner_text(timeout=1000)
        except Exception:
            continue
        digits = "".join(c for c in text if c.isdigit())
        if digits:
            return int(digits)
    return None


def start_ffmpeg(out_path: pathlib.Path, max_seconds: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "pulse", "-i", "vsink.monitor",
         "-ac", "1", "-ar", "16000",
         "-t", str(max_seconds), str(out_path)]
    )


# Экраны, которые стоят между ссылкой и звонком. Ключ — набор селекторов
# из SELECTORS, значение — текст, по исчезновению которого видно, что шаг
# пройден. Проверять именно по тексту обязательно: нажатие может «пройти»
# и ничего не сделать, см. click_through.
PRELUDE_STEPS = [
    ("onboarding_ok", "Большое обновление"),
    ("not_now", "Разрешите находить вас"),
    ("continue_in_browser", "Продолжить в браузере"),
    ("profile_ok", "Всё хорошо"),
]


def on_screen(page, text: str) -> bool:
    try:
        return text in page.inner_text("body")
    except Exception:  # noqa: BLE001
        return False


def click_through(page, key: str, mark: str) -> bool:
    """Нажать так, чтобы это действительно сработало.

    Обычный клик и клик «через силу» отдают событие элементу, который
    лежит сверху: у окна «Большое обновление» кнопку перекрывает
    картинка из того же окна. Исключения при этом не возникает, и мы
    считали такой клик успешным, а окно оставалось. Работает нажатие
    средствами самой страницы, но проверять результат всё равно надо
    по тексту на экране.
    """
    for sel in SELECTORS.get(key, []):
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=2000):
                continue
        except Exception:  # noqa: BLE001
            continue
        for how, act in (
            ("из страницы", lambda el=loc: el.evaluate("e => e.click()")),
            ("обычный", lambda el=loc: el.click(timeout=4000)),
            ("через силу", lambda el=loc: el.click(force=True, timeout=4000)),
        ):
            try:
                act()
                page.wait_for_timeout(2500)
                if not on_screen(page, mark):
                    print(f"пройден экран «{mark}» ({how})")
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def call_frame(page, wait_sec: int = 60):
    """Фрейм с интерфейсом звонка.

    Телемост стал мессенджером, и сам звонок переехал во
    вложенную страницу /private-join/. Снаружи её видно как единый
    экран, но для кода это отдельный документ: в главной странице
    кнопки «Подключиться» нет вовсе — мы её искали там и не находили,
    хотя на снимке экрана она была.
    """
    for _ in range(wait_sec):
        for f in page.frames:
            if "/private-join/" in f.url or "/j/" in f.url and f != page.main_frame:
                return f
        page.wait_for_timeout(1000)
    return None


def by_text(page, text: str, timeout: int = 8000):
    """Видимый элемент с таким текстом, каким бы тегом он ни был."""
    try:
        loc = page.get_by_text(text, exact=True).first
        loc.wait_for(state="visible", timeout=timeout)
        return loc
    except Exception:  # noqa: BLE001
        return None


def prelude(page) -> None:
    """Проходим череду окон до экрана звонка.

    Появилась когда Телемост объединили с Мессенджером.
    Окна показываются по очереди и не все сразу, поэтому идём кругами,
    пока хоть что-то нажимается.
    """
    for _ in range(8):
        done_any = False
        for key, mark in PRELUDE_STEPS:
            if on_screen(page, mark) and click_through(page, key, mark):
                done_any = True
        if not done_any:
            return


def close(browser, ctx) -> None:
    """У постоянного профиля браузера как объекта нет — закрываем контекст."""
    (browser or ctx).close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="Cyber Brief")
    ap.add_argument("--max-min", type=int, default=120)
    ap.add_argument("--leave-silent-min", type=int, default=15,
                    help="уйти, если последние N минут в записи тишина; 0 — не уходить")
    ap.add_argument("--leave-alone-sec", type=int, default=90,
                    help="уйти, если бот остался в комнате один дольше N секунд; 0 — не уходить")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--selectors", help="json-файл, переопределяющий SELECTORS")
    ap.add_argument("--profile", help="каталог профиля браузера с сессией Яндекса")
    args = ap.parse_args()

    if args.selectors:
        SELECTORS.update(json.loads(pathlib.Path(args.selectors).read_text("utf-8")))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    flags = ["--no-sandbox", "--disable-dev-shm-usage",
             "--use-fake-ui-for-media-stream",   # авто-согласие на камеру/микрофон
             "--use-fake-device-for-media-stream",  # пустой микрофон вместо реального
             "--autoplay-policy=no-user-gesture-required",
             # Нам нужен только звук. На встрече с двумя десятками
             # участников браузер иначе тратит память и процессор на
             # декодирование всех входящих видеопотоков, а OOM-kill
             # посреди встречи стоит записи.
             "--renderer-process-limit=2",
             "--disable-gpu", "--disable-software-rasterizer",
             "--disable-background-timer-throttling",
             "--disable-backgrounding-occluded-windows"]

    with sync_playwright() as p:
        if args.profile:
            # Постоянный профиль — в нём живёт сессия Яндекса, и тогда бот
            # заходит своим аккаунтом: со своим именем и аватаркой, а не
            # случайной картинкой, которую Телемост выдаёт гостю.
            browser = None
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=args.profile,
                executable_path="/usr/bin/chromium",
                headless=False, user_agent=CHROME_UA, locale="ru-RU",
                permissions=["microphone", "camera"], args=flags)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=False,  # под Xvfb: headless чаще детектится и ломает WebRTC
                args=flags,
            )
            ctx = browser.new_context(user_agent=CHROME_UA, locale="ru-RU",
                                      permissions=["microphone", "camera"])
            page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        prelude(page)

        # Дальше всё, что касается звонка, ищем во фрейме, а не на
        # странице: снимки экрана и выход из звонка остаются за page.
        ui = call_frame(page) or page
        print("фрейм звонка:", ui.url[:80] if ui is not page else "не найден, работаю со страницей")

        if args.discover:
            page.screenshot(path=f"{args.out}.png", full_page=True)
            pathlib.Path(f"{args.out}.html").write_text(page.content(), encoding="utf-8")
            print(f"DISCOVER saved: {args.out}.png / {args.out}.html")
            close(browser, ctx)
            return 0

        # Имя вводим только гостем. Под своим аккаунтом его спрашивать
        # некому, зато рядом есть поиск мессенджера с такой же разметкой —
        # «имя бота» уехал именно туда, а не в имя участника.
        if not args.profile:
            name_input = first_visible(ui, SELECTORS["name_input"], 5000)
            if name_input:
                name_input.fill(args.name)

        for key in ("mic_toggle", "cam_toggle"):
            btn = first_visible(ui, SELECTORS[key], 3000)
            if btn:
                try:
                    btn.evaluate("e => e.click()")
                except Exception:  # noqa: BLE001
                    pass

        # Кнопка «Завершить звонок» появляется в боковом виджете сразу,
        # ещё до входа, пока идёт «Подключение». Считать её признаком
        # входа нельзя: бот так «вошёл» в пустую комнату
        # и записал две минуты тишины, а люди его не видели.
        # Честный признак — исчезновение самой кнопки «Подключиться».
        join = first_visible(ui, SELECTORS["join_button"], 8000) or \
            by_text(ui, "Подключиться") or by_text(ui, "Присоединиться")
        if not join:
            print("FAIL: кнопка «Подключиться» не найдена — "
                  "запусти --discover и обнови селекторы")
            page.screenshot(path=f"{args.out}.stuck.png")
            close(browser, ctx)
            return 2
        for how, act in (("из страницы", lambda: join.evaluate("e => e.click()")),
                         ("обычный", lambda: join.click(timeout=5000)),
                         ("через силу", lambda: join.click(force=True, timeout=5000))):
            try:
                act()
                page.wait_for_timeout(3000)
                if not on_screen(ui, "Подключиться"):
                    print(f"нажал «Подключиться» ({how})")
                    break
            except Exception:  # noqa: BLE001
                continue

        joined = False
        for _ in range(180):  # до 3 минут: бывает комната ожидания
            if not on_screen(ui, "Подключиться"):
                joined = True
                break
            page.wait_for_timeout(1000)
        if not joined:
            print("FAIL: кнопка «Подключиться» не исчезла за 3 минуты — "
                  "в звонок не попали")
            page.screenshot(path=f"{args.out}.stuck.png")
            close(browser, ctx)
            return 3

        page.wait_for_timeout(4000)
        page.screenshot(path=f"{args.out}.joined.png")
        collect_participants(ui, out, shot=page)
        testids = ui.eval_on_selector_all(
            "[data-testid]", "els => [...new Set(els.map(e => e.dataset.testid))]")
        pathlib.Path(f"{args.out}.testids.json").write_text(
            json.dumps(testids, ensure_ascii=False, indent=1), encoding="utf-8")
        print("JOINED", flush=True)

        max_seconds = args.max_min * 60
        rec = start_ffmpeg(out, max_seconds)
        started = time.time()

        # Диаризация «по-рыночному»: не по голосу, а по индикатору «говорит» в DOM.
        speakers_log = out.with_suffix(".speakers.jsonl").open("w", encoding="utf-8")
        alone_since: float | None = None
        dropped_since: float | None = None
        last_count: int | None = -1
        try:
            stop_flag = out.parent / "STOP"
            while time.time() - started < max_seconds:
                if rec.poll() is not None:
                    break

                # Досрочная остановка снаружи. Именно файлом, а не docker stop:
                # убитый ffmpeg оставляет wav с недописанным заголовком.
                if stop_flag.exists():
                    print("Получен STOP — заканчиваю запись")
                    break

                # Конец встречи по-настоящему: бот остался один. Счётчик
                # участников Телемост рисует прямо в кнопке «Участники».
                if args.leave_alone_sec:
                    n = participant_count(page)
                    if n != last_count:
                        print(f"[{int(time.time() - started):5d}с] участников: {n}",
                              flush=True)
                        last_count = n
                    if n is not None and n <= 1:
                        alone_since = alone_since or time.time()
                        if time.time() - alone_since >= args.leave_alone_sec:
                            print(f"Остался один {args.leave_alone_sec} с — выхожу")
                            break
                    else:
                        alone_since = None

                # Третья проверка, независимая от интерфейса: если последние
                # N минут в записи тишина, писать больше нечего — кто бы там
                # ни числился в комнате.
                if args.leave_silent_min and time.time() - started > 120:
                    peak = recent_peak(out, args.leave_silent_min * 60)
                    if peak is not None and peak < SILENCE_PEAK:
                        print(f"Тишина {args.leave_silent_min} мин "
                              f"(пик {peak}) — выхожу")
                        break

                # Вторая проверка конца: пропала кнопка «завершить звонок».
                # Счётчик участников может не читаться (тогда n is None и
                # первая проверка молчит вечно), а страница при этом уже
                # вывалилась из звонка. 2026-09-01 запись шла все 120 минут
                # лимита, хотя встреча кончилась раньше.
                # Кнопка выхода живёт и в боковом виджете страницы,
                # и во фрейме звонка. Виджет остаётся даже до входа,
                # поэтому спрашиваем фрейм.
                if first_visible(ui, SELECTORS["in_call_marker"], 800):
                    dropped_since = None
                else:
                    dropped_since = dropped_since or time.time()
                    if time.time() - dropped_since >= 60:
                        print("Больше минуты вне звонка — выхожу")
                        break

                try:
                    names = ui.locator(SELECTORS["speaking_marker"]).all_inner_texts()
                except Exception:
                    names = []
                if names:
                    speakers_log.write(json.dumps(
                        {"t": round(time.time() - started, 2), "speaking": names},
                        ensure_ascii=False) + "\n")
                    speakers_log.flush()
                # Конец встречи: экран входа вернулся — значит нас выкинуло.
                try:
                    if ui.locator(SELECTORS["join_button"][0]).first.is_visible():
                        print("Встреча завершена")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(500)
        finally:
            speakers_log.close()
            if rec.poll() is None:
                rec.terminate()
                try:
                    rec.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    rec.kill()
            close(browser, ctx)

    print(f"RESULT file={out} bytes={out.stat().st_size if out.exists() else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
