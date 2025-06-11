import sys
from telethon import TelegramClient
import requests
import re
import html
from .config_private import TELEGRAM_BOT_TOKEN, TELEGRAM_API_HASH, TELEGRAM_API_ID, TELEGRAM_PHONE_NUMBER, TELEGRAM_CHANNEL, TELEGRAM_SESSION_NAME

client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

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

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python telegram_message.py \"<message>\"")
        sys.exit(1)
    # Grab the message from argv
    msg = sys.argv[1]
    # Run the whole thing
    send_telegram(msg)