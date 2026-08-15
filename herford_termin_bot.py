"""
Herford Termin Bot
==================
Перевіряє сайт terminvergabe.krz.de/0230 на появу вільних годин у
конкретних датах і надсилає сповіщення в Telegram.

ВСТАНОВЛЕННЯ:
    pip install playwright requests
    playwright install chromium

ПЕРШИЙ ЗАПУСК (рекомендовано, для перевірки що кроки клікаються правильно):
    python herford_termin_bot.py --once --headed

Якщо все ок (в консолі "OK, дійшли до сторінки з датами") - запускайте у фоні:
    python herford_termin_bot.py

Скрипт зберігає скріншот кожного кроку в папку ./debug/ - якщо щось не
клікається, надішліть мені ці скріншоти і текст помилки, я поправлю селектори.
"""

import argparse
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============================== НАЛАШТУВАННЯ ==============================

CONFIG = {
    # Стартова сторінка запису (крок 1: вибір установи/Standort)
    "START_URL": "https://terminvergabe.krz.de/0230/",

    # Текст, який треба клікнути на кроці вибору установи
    "LOCATION_TEXT": "Servicestelle Internationales",

    # Текст Anliegen (причини звернення), яку треба обрати
    "CONCERN_TEXT": "Ersterfassung Ukraineflüchtlinge",

    # Дати, на які чекаємо вільні години (формат як на сайті: DD.MM.YYYY)
    "TARGET_DATES": ["20.08.2026", "27.08.2026"],

    # Telegram — беремо зі змінних середовища (GitHub Actions secrets), якщо
    # вони є; інакше використовуємо значення нижче (для локального запуску).
    "TELEGRAM_BOT_TOKEN": os.environ.get(
        "TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE"
    ),
    "TELEGRAM_CHAT_ID": os.environ.get(
        "TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE"
    ),  # локально: запустіть get_chat_id.py і впишіть сюди

    # Як часто перевіряти (секунди). Не ставте занадто часто, щоб не заблокували.
    "POLL_INTERVAL_SECONDS": 240,       # ~4 хвилини
    "POLL_JITTER_SECONDS": 60,          # + випадково 0-60 сек, щоб не бути "роботом"

    # Скільки разів повторити спробу кроку, перш ніж здатися
    "STEP_RETRIES": 3,
}

DEBUG_DIR = Path(__file__).parent / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ============================== TELEGRAM ==============================

def send_telegram(text: str) -> None:
    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    if not chat_id or chat_id.startswith("PASTE_"):
        print("[!] TELEGRAM_CHAT_ID не налаштований. Повідомлення НЕ надіслано:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if not r.ok:
            print("[!] Помилка Telegram API:", r.text)
    except Exception as e:
        print("[!] Не вдалося надіслати повідомлення в Telegram:", e)


# ============================== НАВІГАЦІЯ ПО САЙТУ ==============================

def accept_cookies(page):
    for text in ["Akzeptieren", "Zustimmen", "Alle akzeptieren", "OK"]:
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.I))
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                return
        except Exception:
            pass


def click_weiter(page):
    """Натискає кнопку 'Weiter' (Продовжити), якщо вона є."""
    for name in ["Weiter", "Übernehmen", "Bestätigen"]:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{name}", re.I))
            if btn.count() == 0:
                btn = page.get_by_text(re.compile(rf"^{name}\s*$", re.I))
            if btn.count() > 0:
                btn.first.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def screenshot(page, name: str):
    ts = datetime.now().strftime("%H%M%S")
    path = DEBUG_DIR / f"{ts}_{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


def navigate_to_suggestions(page):
    """Проходить кроки 1-3 і опиняється на сторінці 'Terminvorschläge'."""

    page.goto(CONFIG["START_URL"], wait_until="domcontentloaded", timeout=30000)
    accept_cookies(page)
    screenshot(page, "step0_start")

    # Крок 1: вибір установи/локації
    loc = page.get_by_text(re.compile(re.escape(CONFIG["LOCATION_TEXT"]), re.I))
    loc.first.click(timeout=15000)
    screenshot(page, "step1_location")
    click_weiter(page)
    page.wait_for_timeout(1500)

    # Крок 2: вибір причини звернення (Anliegen)
    concern_label = page.get_by_text(re.compile(re.escape(CONFIG["CONCERN_TEXT"]), re.I))
    concern_label.first.click(timeout=15000)
    screenshot(page, "step2_concern")
    click_weiter(page)
    page.wait_for_timeout(1500)

    # Можливий проміжний крок (напр. згода/Datenschutz) - просто тиснемо Weiter,
    # якщо він є, доки не зʼявиться заголовок "Terminvorschläge".
    for _ in range(3):
        if page.get_by_text(re.compile("Terminvorschläge", re.I)).count() > 0:
            break
        if not click_weiter(page):
            break
        page.wait_for_timeout(1500)

    screenshot(page, "step3_suggestions_page")

    if page.get_by_text(re.compile("Terminvorschläge", re.I)).count() == 0:
        raise RuntimeError(
            "Не вдалося дійти до сторінки з пропозиціями термінів. "
            "Перевірте скріншоти в папці debug/."
        )


def check_dates_for_slots(page) -> list[str]:
    """Повертає список цільових дат, для яких на сторінці є доступні (не 'вимкнені') слоти."""

    found_dates = []

    for target_date in CONFIG["TARGET_DATES"]:
        # Заголовок дати, напр. "Donnerstag, 20.08.2026"
        date_header = page.get_by_text(re.compile(re.escape(target_date)))
        if date_header.count() == 0:
            continue  # ця дата взагалі не показана на сторінці (поза діапазоном)

        # Знаходимо контейнер дати і дивимось чи є в ньому активна (не 'disabled') кнопка часу.
        try:
            container = date_header.first.locator(
                "xpath=ancestor::*[self::div or self::section][1]"
            )
            enabled_buttons = container.locator(
                "button:not([disabled]):not(.disabled):not(.inactive)"
            )
            if enabled_buttons.count() > 0:
                found_dates.append(target_date)
        except Exception:
            # якщо не вдалось розпарсити структуру - вважаємо що дата згадана => повідомляємо,
            # краще хибне спрацювання ніж пропущений вільний час
            found_dates.append(target_date)

    return found_dates


# ============================== ОСНОВНИЙ ЦИКЛ ==============================

def run_once(headed: bool) -> bool:
    """Одна перевірка. Повертає True, якщо знайдено вільні слоти."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        try:
            navigate_to_suggestions(page)
            found = check_dates_for_slots(page)
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Перевірено. Знайдено дати зі слотами: {found or '—'}")
            if found:
                msg = (
                    "🎉 Знайдено вільні години на сайті запису Herford!\n"
                    f"Дати: {', '.join(found)}\n"
                    "Бронюйте швидше: " + CONFIG["START_URL"]
                )
                send_telegram(msg)
                return True
            return False
        except Exception as e:
            print("[!] Помилка під час перевірки:", e)
            screenshot(page, "error")
            return False
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Виконати лише одну перевірку і завершити")
    parser.add_argument("--headed", action="store_true", help="Показувати вікно браузера (для налагодження)")
    args = parser.parse_args()

    if args.once:
        run_once(headed=args.headed)
        return

    print("Бот запущено. Перевірка кожні ~"
          f"{CONFIG['POLL_INTERVAL_SECONDS']}-{CONFIG['POLL_INTERVAL_SECONDS']+CONFIG['POLL_JITTER_SECONDS']} сек. Ctrl+C для зупинки.")
    while True:
        found = run_once(headed=args.headed)
        if found:
            print("Слоти знайдено і повідомлення надіслано. Продовжуємо моніторинг "
                  "(бронюйте вручну, бот автоматично не бронює).")
        delay = CONFIG["POLL_INTERVAL_SECONDS"] + random.randint(0, CONFIG["POLL_JITTER_SECONDS"])
        time.sleep(delay)


if __name__ == "__main__":
    main()
