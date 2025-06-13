import json
from .llm_client import llama_chat

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
