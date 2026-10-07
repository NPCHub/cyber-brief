"""Опыт: какое нажатие реально закрывает окна нового Телемоста.

Force-click Playwright исключения не бросает, но событие достаётся
элементу, лежащему сверху, — и мы считали такой клик успешным, хотя окно
оставалось. Поэтому теперь после каждого способа проверяем результат
по тексту на экране.
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]


def screen(page) -> str:
    try:
        return " ".join(page.inner_text("body").split())
    except Exception:  # noqa: BLE001
        return ""


def try_ways(page, sel: str, mark: str, label: str) -> None:
    """Пробуем способы по очереди, пока текст mark не исчезнет с экрана."""
    ways = [
        ("обычный клик", lambda el: el.click(timeout=4000)),
        ("клик через силу", lambda el: el.click(force=True, timeout=4000)),
        ("нажатие из страницы", lambda el: el.evaluate("e => e.click()")),
        ("событие мыши", lambda el: el.evaluate(
            "e => e.dispatchEvent(new MouseEvent('click', "
            "{bubbles: true, cancelable: true, view: window}))")),
    ]
    for name, act in ways:
        if mark not in screen(page):
            print(f"{label}: ушло до способа «{name}»")
            return
        try:
            act(page.locator(sel).first)
            page.wait_for_timeout(2500)
            done = mark not in screen(page)
            print(f"{label}: {name} -> сработало = {done}")
            if done:
                return
        except Exception as e:  # noqa: BLE001
            print(f"{label}: {name} -> {type(e).__name__}")
    print(f"{label}: НЕ ЗАКРЫЛОСЬ ничем")


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium", headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream"])
    ctx = browser.new_context(locale="ru-RU",
                              permissions=["microphone", "camera"])
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)

    try_ways(page, '[data-testid="telemost-3-onboarding-confirm"]',
             "Большое обновление", "приветствие")
    try_ways(page, '[data-testid="meeting-continue-in-browser-continue"]',
             "Продолжить в браузере", "продолжить в браузере")
    page.wait_for_timeout(5000)

    print("адрес:", page.url)
    print("экран:", screen(page)[:300])
    inputs = page.locator("input")
    print("полей ввода:", inputs.count())
    for i in range(min(inputs.count(), 5)):
        el = inputs.nth(i)
        print(f"  поле: placeholder={el.get_attribute('placeholder')!r} "
              f"testid={el.get_attribute('data-testid')!r} видно={el.is_visible()}")
    for i in range(min(page.locator("button").count(), 14)):
        el = page.locator("button").nth(i)
        text = " ".join((el.inner_text() or "").split())[:30]
        if text and el.is_visible():
            print(f"  кнопка: {text!r} testid={el.get_attribute('data-testid')!r}")
    page.screenshot(path="/jobs/discover/guest.png")
    browser.close()
