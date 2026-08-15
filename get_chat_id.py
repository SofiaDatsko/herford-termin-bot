"""
Допоміжний скрипт: визначає ваш Telegram chat_id.

1. Знайдіть у Telegram бота @herford_termin_bot і надішліть йому будь-яке
   повідомлення (наприклад "привіт").
2. Запустіть цей скрипт: python get_chat_id.py
3. Скопіюйте виведений chat_id у herford_termin_bot.py (CONFIG["TELEGRAM_CHAT_ID"]).
"""

import os

import requests

# Локальний одноразовий скрипт - тут можна лишити токен прямо в коді,
# просто НЕ завантажуйте цей файл із заповненим токеном у публічний репозиторій.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8664035319:AAFcz_u0kvFQ0WMWodZZ8bOlmk8qxkbLoBI")

url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
resp = requests.get(url, timeout=15)
data = resp.json()

if not data.get("ok"):
    print("Помилка запиту до Telegram API:", data)
    raise SystemExit(1)

results = data.get("result", [])
if not results:
    print(
        "Повідомлень не знайдено.\n"
        "Спершу напишіть щось боту @herford_termin_bot у Telegram, "
        "а потім запустіть цей скрипт ще раз."
    )
    raise SystemExit(0)

seen = set()
for item in results:
    msg = item.get("message") or item.get("channel_post")
    if not msg:
        continue
    chat = msg["chat"]
    chat_id = chat["id"]
    if chat_id in seen:
        continue
    seen.add(chat_id)
    name = chat.get("username") or chat.get("first_name") or "невідомо"
    print(f"chat_id: {chat_id}   (від: {name})")

print("\nСкопіюйте потрібний chat_id у CONFIG['TELEGRAM_CHAT_ID'] у herford_termin_bot.py")
