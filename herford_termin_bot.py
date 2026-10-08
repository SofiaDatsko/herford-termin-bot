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
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def now_kyiv() -> datetime:
    """Поточний час у Києві (сервери GitHub Actions працюють в UTC, тому
    просто datetime.now() показував би плутанину в повідомленнях)."""
    return datetime.now(KYIV_TZ)

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

    # Назва категорії-акордеону, в якій лежить CONCERN_TEXT (треба розкрити спершу)
    "CONCERN_CATEGORY_TEXT": "Ukraineflüchtlinge",

    # Дати, на які чекаємо вільні години (формат як на сайті: DD.MM.YYYY)
    "TARGET_DATES": ["15.10.2026", "22.10.2026"],

    # Telegram — беремо зі змінних середовища (GitHub Actions secrets), якщо
    # вони є; інакше використовуємо значення нижче (для локального запуску).
    "TELEGRAM_BOT_TOKEN": os.environ.get(
        "TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE"
    ),
    "TELEGRAM_CHAT_ID": os.environ.get(
        "TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE"
    ),  # локально: запустіть get_chat_id.py і впишіть сюди

    # Як часто перевіряти при ЛОКАЛЬНОМУ безкінечному запуску (python herford_termin_bot.py --forever)
    "POLL_INTERVAL_SECONDS": 240,       # ~4 хвилини
    "POLL_JITTER_SECONDS": 60,          # + випадково 0-60 сек, щоб не бути "роботом"

    # Раз на годину відправляти "ще нічого" (heartbeat). Спрацьовує лише в
    # тому запуску, час якого потрапляє в перші HEARTBEAT_WINDOW_MINUTES
    # хвилин години (напр. 10:00-10:01). Має бути МЕНШЕ за інтервал зовнішніх
    # запусків (рекомендовано 3 хв), інакше heartbeat надішлеться кілька разів
    # поспіль в межах однієї години.
    "HEARTBEAT_WINDOW_MINUTES": 2,

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


def robust_click(page, text: str, step_name: str = "click"):
    """
    Намагається клікнути елемент з даним текстом кількома способами,
    бо на держ. сайтах часто клікабельний не сам текст, а чекбокс/label
    навколо нього.
    """
    node = page.get_by_text(re.compile(re.escape(text), re.I)).first
    node.scroll_into_view_if_needed(timeout=10000)

    # 1. Прямий клік по тексту
    try:
        node.click(timeout=5000)
        return True
    except Exception:
        pass

    # 2. Клік по найближчому предку label/li/div/tr (частий випадок для чекбоксів)
    for tag in ["label", "li", "tr", "div"]:
        try:
            ancestor = node.locator(f"xpath=ancestor::{tag}[1]")
            if ancestor.count() > 0:
                ancestor.first.click(timeout=5000)
                return True
        except Exception:
            continue

    # 3. Пошук пов'язаного checkbox/radio input поруч і клік по ньому
    try:
        container = node.locator("xpath=ancestor::*[self::div or self::li or self::tr][1]")
        input_el = container.locator("input[type=checkbox], input[type=radio]")
        if input_el.count() > 0:
            input_el.first.click(timeout=5000, force=True)
            return True
    except Exception:
        pass

    # 4. Форсований клік напряму по тексту (ігнорує перевірку видимості)
    try:
        node.click(timeout=5000, force=True)
        return True
    except Exception:
        pass

    screenshot(page, f"FAILED_{step_name}")
    return False


def find_stepper_plus_button(row_text_locator):
    """Шукає кнопку '+' степпера, перевіряючи кілька рівнів батьківських
    елементів угору (розмітка рядків з Anliegen може бути вкладена по-різному)."""
    for level in range(1, 7):
        container = row_text_locator.locator(f"xpath=ancestor::*[{level}]")
        try:
            if container.count() == 0:
                continue
            btns = container.locator("button")
            if btns.count() > 0:
                return btns.last
        except Exception:
            continue
    return None


def navigate_to_suggestions(page):
    """Проходить кроки 1-3 і опиняється на сторінці 'Terminvorschläge'."""

    page.goto(CONFIG["START_URL"], wait_until="domcontentloaded", timeout=30000)
    accept_cookies(page)
    screenshot(page, "step0_start")

    # Крок 1: вибір установи/локації
    if not robust_click(page, CONFIG["LOCATION_TEXT"], "step1_location"):
        raise RuntimeError(f"Не вдалося клікнути локацію '{CONFIG['LOCATION_TEXT']}'")
    screenshot(page, "step1_location")
    click_weiter(page)
    page.wait_for_timeout(1500)

    # Крок 2: вибір причини звернення (Anliegen)
    # На цьому сайті категорії згорнуті в акордеон - спершу відкриваємо
    # категорію, потім тиснемо кнопку "+" біля потрібного Anliegen
    # (сам текст не клікабельний, кількість регулюється степпером -/+).
    if not robust_click(page, CONFIG["CONCERN_CATEGORY_TEXT"], "step2_category"):
        raise RuntimeError(
            f"Не вдалося розкрити категорію '{CONFIG['CONCERN_CATEGORY_TEXT']}'"
        )
    page.wait_for_timeout(500)
    screenshot(page, "step2_category_expanded")

    concern_row = page.get_by_text(
        re.compile(rf"^{re.escape(CONFIG['CONCERN_TEXT'])}", re.I)
    ).first
    concern_row.scroll_into_view_if_needed(timeout=10000)

    plus_button = find_stepper_plus_button(concern_row)
    if plus_button is not None:
        plus_button.click(timeout=10000, force=True)
    else:
        # Резервний варіант: якщо кнопку "+" не знайдено, спробуємо
        # напряму вписати "1" у поле кількості поруч.
        row_container = concern_row.locator("xpath=ancestor::*[3]")
        number_input = row_container.locator("input")
        if number_input.count() > 0:
            number_input.last.fill("1")
        else:
            screenshot(page, "FAILED_no_plus_button")
            raise RuntimeError(
                "Не вдалося знайти кнопку '+' або поле кількості для Anliegen"
            )
    screenshot(page, "step2_concern")
    click_weiter(page)
    page.wait_for_timeout(1500)

    # Крок 3: вибір локації (у цього Anliegen зазвичай лише один варіант,
    # тому просто тиснемо Weiter; якщо є картка локації для вибору - клікаємо її).
    try:
        robust_click(page, "Servicestelle Internationales der Hansestadt Herford", "step3_standort")
        page.wait_for_timeout(500)
    except Exception:
        pass
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
        # Заголовок дати, напр. "Donnerstag, 15.10.2026"
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

def maybe_send_heartbeat(already_sent: bool) -> bool:
    """Раз на годину (у вікні перших HEARTBEAT_WINDOW_MINUTES хвилин) шле
    коротке 'ще нічого'. already_sent - щоб не слати двічі в межах одного
    запуску скрипта (CI-цикл робить кілька перевірок за один запуск)."""
    if already_sent:
        return True
    now = now_kyiv()
    if now.minute < CONFIG["HEARTBEAT_WINDOW_MINUTES"]:
        dates_text = " / ".join(CONFIG["TARGET_DATES"])
        send_telegram(f"🤖 Бот живий. Станом на {now:%H:%M} вільних годин на {dates_text} ще немає.")
        return True
    return False


def run_once(headed: bool) -> bool:
    """Одна перевірка. Повертає True, якщо знайдено вільні слоти."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        try:
            navigate_to_suggestions(page)
            found = check_dates_for_slots(page)
            print(f"[{now_kyiv():%Y-%m-%d %H:%M:%S}] Перевірено. Знайдено дати зі слотами: {found or '—'}")
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


def run_ci_check(headed: bool):
    """Одна перевірка + перевірка heartbeat. Викликається як --once,
    так і без флагів - зовнішній сервіс (cron-job.org) задає ритм запусків."""
    found = run_once(headed=headed)
    maybe_send_heartbeat(already_sent=False)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Виконати лише одну перевірку і завершити (для тесту)")
    parser.add_argument("--forever", action="store_true", help="Безкінечний цикл для локального запуску (не для GitHub Actions)")
    parser.add_argument("--headed", action="store_true", help="Показувати вікно браузера (для налагодження)")
    args = parser.parse_args()

    if args.forever:
        print("Бот запущено (локальний безкінечний режим). Перевірка кожні ~"
              f"{CONFIG['POLL_INTERVAL_SECONDS']}-{CONFIG['POLL_INTERVAL_SECONDS']+CONFIG['POLL_JITTER_SECONDS']} сек. Ctrl+C для зупинки.")
        while True:
            run_once(headed=args.headed)
            delay = CONFIG["POLL_INTERVAL_SECONDS"] + random.randint(0, CONFIG["POLL_JITTER_SECONDS"])
            time.sleep(delay)
        return

    # І --once, і запуск без флагів роблять одну перевірку + heartbeat.
    # Зовнішній сервіс (cron-job.org) сам задає точний ритм викликів.
    run_ci_check(headed=args.headed)


if __name__ == "__main__":
    main()
