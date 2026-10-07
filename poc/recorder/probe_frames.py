"""Где именно живёт кнопка «Подключиться»: в основной странице или во фрейме."""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]
STEPS = [
    ('[data-testid="telemost-3-onboarding-confirm"]', "Большое обновление"),
    ('[data-testid="independent-onboarding-modal-modal"] button:has-text("Не сейчас")',
     "Разрешите находить вас"),
    ('[data-testid="meeting-continue-in-browser-continue"]', "Продолжить в браузере"),
    ('button:has-text("Всё хорошо")', "Всё хорошо"),
]


def body(page) -> str:
    try:
        return " ".join(page.inner_text("body").split())
    except Exception:  # noqa: BLE001
        return ""


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="/profile", executable_path="/usr/bin/chromium",
        headless=False, locale="ru-RU", permissions=["microphone", "camera"],
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)

    for _ in range(6):
        acted = False
        for sel, mark in STEPS:
            if mark in body(page):
                try:
                    page.locator(sel).first.evaluate("e => e.click()")
                    page.wait_for_timeout(2500)
                    if mark not in body(page):
                        acted = True
                except Exception:  # noqa: BLE001
                    pass
        if not acted:
            break
    page.wait_for_timeout(8000)

    print("фреймов:", len(page.frames))
    for f in page.frames:
        try:
            text = " ".join(f.locator("body").inner_text().split())
        except Exception as e:  # noqa: BLE001
            text = f"<не прочитался: {type(e).__name__}>"
        has = "Подключиться" in text
        print(f"  фрейм url={f.url[:70]!r} есть кнопка={has} текст={text[:90]!r}")
        if has:
            found = f.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll('*')) {
                    if ((el.innerText || '').trim() === 'Подключиться') {
                        const r = el.getBoundingClientRect();
                        out.push({tag: el.tagName, cls: (el.className||'').toString().slice(0,50),
                                  testid: el.getAttribute('data-testid'),
                                  aria: el.getAttribute('aria-label'),
                                  w: Math.round(r.width), h: Math.round(r.height)});
                    }
                }
                return out;
            }""")
            for el in found:
                print("    ", el)
    page.screenshot(path="/jobs/discover/frames.png")
    ctx.close()
