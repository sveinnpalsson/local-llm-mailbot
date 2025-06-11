import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, List

import requests

from .config import (
    DEEP_MAX_INPUT_TOKENS,
    DEEP_MAX_OUTPUT_TOKENS,
    DEEP_THRESHOLD_IMPORTANCE,
    INITIAL_MAX_INPUT_TOKENS,
    INITIAL_MAX_OUTPUT_TOKENS,
    LABELS,
    LLAMA_SERVER_MODEL,
    LLAMA_SERVER_URL,
)
from .config_private import (
    ACCOUNTS,
    USER_PERSONAL_IGNORE_CLAUSE,
    USER_PROFILE_LLM_PROMPT,
    USER_PROFILE_LLM_PROMPT_DEEP,
)
from .db import get_conn, get_ignore_rules


conn = get_conn()

# TODO: This part is under construction - we want ignore rules to be read from the database but we currently never add ignore rules to it.
ignore_list = get_ignore_rules(conn)
ignore_clause = ""
if ignore_list:
    ignore_clause = (
      "The following are special ignore rules:\n"
      + "\n".join(f"- {p}" for p in ignore_list)
      + "\n\n"
    )

ignore_clause = USER_PERSONAL_IGNORE_CLAUSE

# System prompt for the shallow-analysis
_system = [{
    "role": "system",
    "content": (
        "You are an email assistant for user who receives a lot of email."
       f"{USER_PROFILE_LLM_PROMPT}"
        "You may think step-by-step and show your reasoning "
        "(wrapped in <think>…</think>), but at the end you must output *only* a JSON object. "
        "That JSON must have exactly these fields:\n"
        f"  category: one of {LABELS}\n"
        "  importance: integer 1–10 (within category)\n"
        "  action: short instruction, if any is likely needed from the User (e.g. 'Reply to confirm' or 'Add event to calendar' or 'Pay bill')\n"
        "  summary: one- or two-sentence overview of the email plus an explanation of WHY to take the mentioned action if one is detected.\n"
        "Do not output anything else."
        "Do you best to omit sensitive information in your answer."
    )
}]
# System prompt for the deep analysis. 
# TODO: try different prompts.
_system_deep = [{
    "role": "system",
    "content": (
        "You are a final stage email assistant for user who receives a lot of email."
        f"{USER_PROFILE_LLM_PROMPT_DEEP}"
        "You may think step-by-step and show your reasoning "
        "(wrapped in <think>…</think>), but at the end you must output *only* a JSON object. "
        "That JSON must have exactly these fields:\n"
        f"  category: one of {LABELS}\n"
        "  importance: integer 1–10 (within category)\n"
        "  action: short instruction, if any is likely needed from the User (e.g. 'Reply to confirm' or 'Add <event>, <datetime> to calendar' or 'Set reminder for <> at <datetime>')\n"
        "  summary: one- or two-sentence overview of the email plus an explanation of WHY to take the mentioned action if one is detected.\n"
        "Do not output anything else."
        "Do you best to omit sensitive information in your answer."
    )
}]


def extract_json_objects(text: str) -> List[Dict]:
    """
    Scan `text` for all top-level JSON objects and return a list
    of dicts parsed from them. Silently skips any malformed JSON.
    """
    objs = []
    i = 0
    n = len(text)
    while True:
        # find the next opening brace
        start = text.find('{', i)
        if start == -1:
            break

        depth = 0
        for j in range(start, n):
            ch = text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                # when we close the outermost brace, extract
                if depth == 0:
                    candidate = text[start:j+1]
                    try:
                        obj = json.loads(candidate)
                        objs.append(obj)
                    except json.JSONDecodeError:
                        # skip malformed JSON
                        pass
                    # move i past this object and continue scanning
                    i = j + 1
                    break
        else:
            # ran out of string without closing
            break

    return objs


def llama_chat(
    messages: list[dict],
    max_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    presence_penalty: float = 1.2,
    retries: int = 4,
    timeout: float = 120.0
) -> dict | None:
    """
    Sends a `messages` list to llama-server, retries on errors,
    extracts the final JSON object, parses it, and returns a dict.
    """
    for attempt in range(1, retries+1):
        message_len = sum([len(k['content']) for k in messages])
        payload = {
            "model":            LLAMA_SERVER_MODEL,
            "messages":         messages,
            "temperature":      temperature,
            "max_tokens":       max_tokens,
            "top_p":            top_p,
            "presence_penalty": presence_penalty
        }
        logging.debug("LLM payload (attempt %d): %s", attempt, payload)

        t0 = time.time()
        try:
            resp = requests.post(
                f"{LLAMA_SERVER_URL}/v1/chat/completions",
                json=payload,
                timeout=timeout
            )
            resp.raise_for_status()
        except KeyboardInterrupt:
            logging.warning("Aborted by user during LLM call (attempt %d)", attempt)
            raise
        except requests.exceptions.RequestException as e:
            logging.error("LLM request error on attempt %d: %s", attempt, e)
            continue
        
        print(f"Llama task finished: Input length: {message_len} Time: {time.time() - t0:.2f} seconds")
        raw = resp.json()["choices"][0]["message"]["content"]
        logging.debug("LLM raw output (attempt %d): %s", attempt, raw)

        js = extract_json_objects(raw)
        if not js:
            logging.warning("No JSON found on attempt %d, retrying...", attempt)
            continue

        try:
            return js
        except json.JSONDecodeError as e:
            logging.error("JSON parse error on attempt %d: %s\nJSON was: %s",
                          attempt, e, js)
            continue

    logging.error("All %d LLM attempts failed.", retries)
    return None

def load_contact_profile(conn, email):
    row = conn.execute(
      "SELECT profile_json FROM contacts WHERE email=?", (email,)
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def initial_classify(subject, snippet, from_addr, to_addr, date_iso, age_days):
    """
    Fast, shallow classification using only subject+snippet.
    """
    prompt = (
      "/think\n"
      f"Date: \"{date_iso}\"  Age: {age_days:.2f} days\n"
      f"From: \"{from_addr}\"  To: \"{to_addr}\"\n"
      f"Subject: \"{subject}\"\n"
      f"Snippet: \"{snippet}\"\n\n"
      "When done, output only the JSON object with fields: "
      "category, importance, action, summary."
    )
    messages = _system + [{"role":"user","content": prompt}]
    try:
        last = llama_chat(messages, max_tokens=8192, retries=4)[0]
        return last
    except:
        return {"category":"Spam","importance":1,"action":"","summary":""}


def deep_analyze(subject, body, from_addr, to_addr, date_iso, age_days,
                 init_cat, init_imp, init_act, init_sum, contact_profile_sender="", contact_profile_recipient=""):
    """
    Full deep pass on bodies deemed important.
    """
    # Prepare profile JSON or placeholder
    sender_profile = (
        json.dumps(contact_profile_sender, indent=2)
        if contact_profile_sender else "None"
    )
    recipient_profile = (
        json.dumps(contact_profile_recipient, indent=2)
        if contact_profile_recipient else "None"
    )

    # Build the prompt
    prompt = (
        "/think\n"
        f"Date: \"{date_iso}\"  Age: {age_days:.2f} days\n"
        f"From: \"{from_addr}\"  To: \"{to_addr}\"\n"
        
        "Contact profile (sender) from our records:\n"
        f"{sender_profile}\n\n"
                
        f"Subject: \"{subject}\"\n"
        f"Body: \"{body}\"\n\n"

        "You have these initial fields from the fast pass:\n"
        f"  category: {init_cat}\n"
        f"  importance: {init_imp}\n"
        f"  action: {init_act}\n"
        f"  summary: {init_sum}\n\n"

        "You may adjust any of those based on the full body above, "
        "but do not rename fields. After your <think>…</think> reasoning, "
        "output *only* the final JSON object with exactly these keys:\n"
        "  category, importance, action, summary, deep_summary\n"
    )
    messages = _system_deep + [{"role":"user","content": prompt}]
    try:
        last = llama_chat(messages, max_tokens=8192)[0]
        return last
    except:
        return {
        "category":   init_cat,
        "importance": init_imp,
        "action":     init_act,
        "summary":    init_sum,
        }
