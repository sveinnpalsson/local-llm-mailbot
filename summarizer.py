import json
import requests
from config import LLAMA_SERVER_URL, LLAMA_SERVER_MODEL

_system_summarize = [
  { "role": "system", "content":
    "You are an expert email summarizer. Use chain-of-thought to be precise."
  }
]

def summarize_email(subject: str, snippet: str) -> str:
    content = (
        "/think\n"
        f"Subject: \"{subject}\"\n"
        f"Preview: \"{snippet}\"\n\n"
        "Summarize this email in one or two sentences, focusing on key info."
    )
    payload = {
      "model": LLAMA_SERVER_MODEL,
      "messages": _system_summarize + [{"role":"user","content":content}],
      "temperature": 0.7,
      "max_tokens": 64,
    }
    r = requests.post(f"{LLAMA_SERVER_URL}/v1/chat/completions", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def digest_today(items: list[dict]) -> str:
    """
    items: [
      {subject, category, importance, action, summary}, ...
    ]
    """
    # Build JSON array for the model
    arr = json.dumps(items, indent=2)
    content = (
        "/think\n"
        "Here is today's email data as JSON:\n" + arr + "\n\n"
        "Produce a bullet-list digest grouped by category. "
        "Under each category, list the top 3 actions by importance."
    )
    payload = {
      "model": LLAMA_SERVER_MODEL,
      "messages": _system_summarize + [{"role":"user","content":content}],
      "temperature": 0.7,
      "max_tokens": 256,
    }
    r = requests.post(f"{LLAMA_SERVER_URL}/v1/chat/completions", json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
