"""Вход в аккаунт Яндекса для бота — по QR-коду, без пароля на сервере.

Зачем: гостю Телемост назначает случайную аватарку из стандартных и не даёт
писать в чат. Имя и аватар берутся из аккаунта, поэтому боту нужен вход.

Пароль при этом нигде не появляется: страница входа показывает QR-код,
мы отдаём его картинкой в Telegram, владелец сканирует телефоном. На сервере
остаётся только сессия в профиле браузера.

    python3 yandex_login.py --profile /profile --shot /profile/login.png
    python3 yandex_login.py --profile /profile --check
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

from playwright.sync_api import sync_playwright

CHROME_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
AUTH_URL = "https://passport.yandex.ru/auth"
# Список аккаунтов сессии. Вход по QR подтверждает тот аккаунт, который
# активен в приложении на телефоне, а если к номеру привязано несколько —
# в браузер попадают все, и рабочим становится основной. Телемост берёт
# именно активный, поэтому после входа его надо выбрать явно.
LIST_URL = "https://passport.yandex.ru/auth/list"
ID_URL = "https://id.yandex.ru/"
# Ссылка на вход по QR подписана по-разному в разных сборках паспорта,
# поэтому ищем по нескольким формулировкам, а не по одному селектору.
QR_HINTS = ["QR-код", "QR-коду", "QR code"]


def browser(p, profile: str):
    """Профиль на диске — в нём и живёт сессия между запусками."""
    return p.chromium.launch_persistent_context(
        user_data_dir=profile,
        executable_path="/usr/bin/chromium",
        headless=False,  # под Xvfb: headless чаще ловится защитой
        user_agent=CHROME_UA, locale="ru-RU",
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--disable-software-rasterizer"],
    )


def matches(want: str, got: str) -> bool:
    """Логин без домена — то же самое, что адрес: сравниваем по нему."""
    if not want:
        return True
    return bool(got) and want.split("@")[0] == got.split("@")[0]


def pick_account(page, want: str) -> bool:
    """Делаем нужный аккаунт активным в сессии браузера."""
    if not want:
        return True
    try:
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "/auth/list" not in page.url:
            return True  # аккаунт в сессии один, выбирать не из чего
        # Ищем и по полному адресу, и по логину: список показывает то одно,
        # то другое в зависимости от сборки.
        for needle in (want, want.split("@")[0]):
            tile = page.get_by_text(needle, exact=False).first
            if tile.is_visible():
                tile.click()
                page.wait_for_timeout(4000)
                return True
    except Exception as e:  # noqa: BLE001
        print(f"выбрать аккаунт не удалось: {type(e).__name__}: {e}")
    return False


def account_name(page) -> str:
    """Кто сейчас активен в сессии.

    Яндекс ID не печатает адрес почты на странице целиком, зато показывает
    логин отдельной строкой — по нему и опознаём аккаунт. Раньше искали
    адрес регулярным выражением и всегда получали пустоту.
    """
    try:
        body = page.inner_text("body")[:4000]
    except Exception:  # noqa: BLE001
        return ""
    mail = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}", body)
    if mail:
        return mail.group(0)
    for line in (l.strip() for l in body.splitlines()):
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,30}", line):
            return line
    return ""


def check(profile: str, want: str = "") -> int:
    """Жива ли сессия. Пишем ответ в session.json — его читает /status."""
    state = {"checked": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False}
    with sync_playwright() as p:
        ctx = browser(p, profile)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(ID_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            # Разлогиненного Яндекс уводит на страницу входа — по адресу
            # это видно надёжнее, чем по вёрстке личного кабинета.
            state["ok"] = "/auth" not in page.url
            state["account"] = account_name(page) if state["ok"] else ""
            if state["ok"] and want and not matches(want, state["account"]):
                # Активным стал не тот аккаунт — на встрече это увидели бы
                # по чужому имени и аватарке.
                pick_account(page, want)
                page.goto(ID_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                state["account"] = account_name(page)
            state["wanted"] = want
            state["right"] = matches(want, state["account"])
        except Exception as e:  # noqa: BLE001
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            ctx.close()
    pathlib.Path(profile, "session.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False))
    return 0 if state["ok"] else 1


def login(profile: str, shot: str, wait_sec: int, want: str = "") -> int:
    with sync_playwright() as p:
        ctx = browser(p, profile)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if "/auth" not in page.url:
                print("уже выполнен вход")
                return check_after(page, profile, want)

            for hint in QR_HINTS:
                try:
                    link = page.get_by_text(hint, exact=False).first
                    if link.is_visible():
                        link.click()
                        page.wait_for_timeout(3000)
                        break
                except Exception:  # noqa: BLE001
                    continue
            else:
                print("ссылку на вход по QR не нашёл — отдаю экран как есть")

            page.screenshot(path=shot)
            print(f"SHOT {shot}", flush=True)

            # Ждём, пока владелец подтвердит вход телефоном. Код живёт
            # около минуты, страница обновляет его сама — поэтому каждые
            # 55 секунд переснимаем экран: в чат уйдёт действующий код,
            # а не протухший снимок первой минуты.
            deadline = time.time() + wait_sec
            last_shot = time.time()
            while time.time() < deadline:
                if "/auth" not in page.url:
                    page.wait_for_timeout(4000)
                    return check_after(page, profile, want)
                if time.time() - last_shot > 55:
                    page.screenshot(path=shot)
                    last_shot = time.time()
                    print(f"SHOT {shot}", flush=True)
                time.sleep(3)
            print("FAIL: вход не подтверждён за отведённое время")
            return 3
        finally:
            ctx.close()


def check_after(page, profile: str, want: str = "") -> int:
    """Фиксируем удачный вход сразу, чтобы /status не ждал ночной проверки."""
    try:
        pick_account(page, want)
        page.goto(ID_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
    except Exception:  # noqa: BLE001
        pass
    name = account_name(page)
    pathlib.Path(profile, "session.json").write_text(json.dumps(
        {"checked": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
         "account": name, "wanted": want,
         "right": matches(want, name)},
        ensure_ascii=False), encoding="utf-8")
    print(f"OK вход выполнен: {name or 'аккаунт'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="/profile")
    ap.add_argument("--shot", default="/profile/login.png")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wait-sec", type=int, default=180)
    ap.add_argument("--account", default="",
                    help="какой аккаунт сделать активным, например bot@yandex.ru")
    args = ap.parse_args()
    pathlib.Path(args.profile).mkdir(parents=True, exist_ok=True)
    return check(args.profile, args.account) if args.check else login(
        args.profile, args.shot, args.wait_sec, args.account)


if __name__ == "__main__":
    sys.exit(main())
