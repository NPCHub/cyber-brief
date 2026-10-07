"""Снимаем селекторы с экрана входа в звонок нового Телемоста."""
import re
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]
STEPS = [
    ('[data-testid="telemost-3-onboarding-confirm"]', "Большое обновление"),
    ('[data-testid="independent-onboarding-modal-modal"] button',
     "Разрешите находить вас"),
    ('[data-testid="meeting-continue-in-browser-continue"]',
     "Продолжить в браузере"),
    ('button:has-text("Всё хорошо")', "Всё хорошо"),
]


def screen(page) -> str:
    try:
        return " ".join(page.inner_text("body").split())
    except Exception:  # noqa: BLE001
        return ""


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

    for _ in range(6):
        acted = False
        for sel, mark in STEPS:
            if mark in screen(page):
                try:
                    page.locator(sel).first.evaluate("e => e.click()")
                    page.wait_for_timeout(2500)
                    if mark not in screen(page):
                        print(f"пройдено: {mark}")
                        acted = True
                except Exception:  # noqa: BLE001
                    pass
        if not acted:
            break

    page.wait_for_timeout(6000)
    html = page.content()
    print("есть «Подключиться»:", "Подключиться" in html)

    # Ищем всё кликабельное с нужным текстом, не полагаясь на тег button.
    found = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('button, [role=button], a')) {
            const t = (el.innerText || '').trim().slice(0, 30);
            const box = el.getBoundingClientRect();
            if (box.width < 5 || box.height < 5) continue;
            out.push({
                text: t,
                testid: el.getAttribute('data-testid'),
                aria: el.getAttribute('aria-label'),
                cls: (el.className || '').toString().slice(0, 60),
                tag: el.tagName,
            });
        }
        return out;
    }""")
    print("--- видимые элементы управления:")
    for f in found:
        print(f"  {f['tag']} text={f['text']!r} testid={f['testid']!r} "
              f"aria={f['aria']!r} class={f['cls']!r}")

    page.screenshot(path="/jobs/discover/join.png")
    ctx.close()
