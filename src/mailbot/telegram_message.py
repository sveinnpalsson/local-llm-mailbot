import sys
from telethon import TelegramClient
import requests
import re
import html
from typing import Dict, List, Any

from .config_private import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def escape_markdown(text: str) -> str:
    # Characters to escape in Markdown
    special = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(r'([%s])' % re.escape(special), r'\\\1', text)

def send_telegram(text: str, html: bool = False):
    """
    Sends `text` to your Telegram chat.
    If html=True, uses HTML parse mode; otherwise Markdown.
    """
    if not html:
        safe_msg = escape_markdown(text)

    payload = {
        "chat_id":                  TELEGRAM_CHANNEL,
        "text":                     safe_msg,
        "parse_mode":               "HTML" if html else "Markdown",
        "disable_notification":     False,
        "disable_web_page_preview": True
    }
    # Send as JSON in the POST body
    resp = requests.post(API_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_telegram_with_buttons(text: str, buttons: List[Dict[str,str]]):
    """
    Use Telegram Bot API to send a message with inline keyboard.
    Buttons: [{"text": "...", "callback_data": "..."}]
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "reply_markup": {"inline_keyboard": [buttons]}
    }
    requests.post(url, json=payload)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python telegram_message.py \"<message>\"")
        sys.exit(1)
        
    # Grab the message from argv
    msg = sys.argv[1]
    # Run the whole thing
    send_telegram(msg)