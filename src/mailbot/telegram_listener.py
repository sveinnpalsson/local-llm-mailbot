# src/mailbot/telegram_listener.py

import threading
import time
import queue
import requests

from .config_private import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL

# Bot API base URL
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# In-memory queue for callbacks (yes/no replies)
response_queue: queue.Queue[str] = queue.Queue()
_update_offset = 0  # for getUpdates offset


def _poll_updates():
    global _update_offset
    while True:
        try:
            params = {"offset": _update_offset, "timeout": 30}
            resp = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=40)
            data = resp.json().get("result", [])
        except Exception as e:
            print("❌ Poll error:", e)
            time.sleep(1)
            continue

        for update in data:
            _update_offset = update["update_id"] + 1

            # 1) Inline-button callback (yes/no)
            cb = update.get("callback_query")
            if cb and str(cb["message"]["chat"]["id"]) == str(TELEGRAM_CHANNEL):
                choice = cb.get("data")
                if choice:
                    response_queue.put(choice)  # "yes" or "no"
                # Acknowledge so Telegram stops the spinner
                requests.get(
                    f"{BASE_URL}/answerCallbackQuery",
                    params={"callback_query_id": cb["id"]},
                    timeout=5
                )
                continue

            # 2) Plain‐text message reply
            msg = update.get("message")
            if msg and str(msg.get("chat", {}).get("id")) == str(TELEGRAM_CHANNEL):
                text = msg.get("text")
                if text:
                    # Push the raw text into the same queue
                    response_queue.put(text)
                continue

        time.sleep(1)


def start_listener():
    """
    Launches a background thread to poll getUpdates for callbacks & messages.
    Call this once (e.g. before main_loop()).
    """
    t = threading.Thread(target=_poll_updates, daemon=True)
    t.start()


def fetch_latest_user_reply() -> str | None:
    """
    Non-blocking pull from the reply queue.
    Returns the next callback_data ("yes"/"no") or text reply if available, else None.
    """
    try:
        return response_queue.get_nowait()
    except queue.Empty:
        return None