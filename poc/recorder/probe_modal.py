"""Опыт: доходит ли бот до звонка, если нажимать средствами страницы.

Выяснили: обычный клик и клик «через силу» достаются элементу, лежащему
поверх кнопки, и молча ничего не делают. Работает el.click() из самой
страницы. Проверяем весь путь с аккаунтом до экрана звонка.
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]
STEPS = [
    ('[data-testid="telemost-3-onboarding-confirm"]', "Большое обновление"),
    ('[data-testid="independent-onboarding-modal-modal"] button',
     "Разрешите находить вас"),
    ('[data-testid="meeting-continue-in-browser-continue"]',
     "Продолжить в браузере"),
    # Подтверждение профиля: «имя бота, телефон, меня могут найти те…»
    ("button:has-text(\"Всё хорошо\")", "Всё хорошо"),
]


def screen(page) -> str:
    try:
        return " ".join(page.inner_text("body").split())
    except Exception:  # noqa: BLE001
        return ""


def click_in_page(page, sel: str) -> bool:
    try:
        page.locator(sel).first.evaluate("e => e.click()")
        return True
    except Exception:  # noqa: BLE001
        return False


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="/profile", executable_path="/usr/bin/chromium",
        headless=False, locale="ru-RU",
        permissions=["microphone", "camera"],
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)

    # Окна появляются чередой, поэтому проходим список несколько раз.
    for _ in range(6):
        acted = False
        for sel, mark in STEPS:
            if mark in screen(page) and click_in_page(page, sel):
                page.wait_for_timeout(2500)
                if mark not in screen(page):
                    print(f"пройдено: {mark}")
                    acted = True
        if not acted:
            break

    for sec in (5, 10, 15, 25):
        page.wait_for_timeout(5000)
        print(f"--- через {sec} с: {screen(page)[:200]}")

    print("адрес:", page.url)
    for i in range(min(page.locator("button").count(), 20)):
        el = page.locator("button").nth(i)
        try:
            text = " ".join((el.inner_text() or "").split())[:30]
            if text and el.is_visible():
                print(f"  кнопка: {text!r} testid={el.get_attribute('data-testid')!r}")
        except Exception:  # noqa: BLE001
            pass
    inputs = page.locator("input")
    print("полей ввода:", inputs.count())
    page.screenshot(path="/jobs/discover/probe.png")
    ctx.close()
