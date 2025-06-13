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
            cb = update.get("callback_query")
            if cb and str(cb["message"]["chat"]["id"]) == str(TELEGRAM_CHANNEL):
                # enqueue the user's button click data ("yes"/"no")
                response_queue.put(cb["data"])
                # acknowledge the callback so the spinner stops
                requests.get(
                    f"{BASE_URL}/answerCallbackQuery",
                    params={"callback_query_id": cb["id"]},
                    timeout=5
                )

        time.sleep(1)


def start_listener():
    """
    Launches a background thread to poll getUpdates for callback queries.
    Call this once (e.g. before main_loop()).
    """
    t = threading.Thread(target=_poll_updates, daemon=True)
    t.start()


def fetch_latest_user_reply() -> str | None:
    """
    Non-blocking pull from the callback queue.
    Returns 'yes' or 'no' if available, else None.
    """
    try:
        return response_queue.get_nowait()
    except queue.Empty:
        return None
