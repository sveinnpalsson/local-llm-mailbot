import json
import logging
from datetime import datetime
from .classifier import llama_chat
from tqdm import tqdm

SYSTEM = {
    "role": "system",
    "content": (
        "You are an assistant that turns a single email description into either "
        "a calendar event or a reminder.  You will see one email at a time, "
        "and must respond *only* with a JSON object with exactly these keys:\n"
        '  type:         "event" or "reminder"\n'
        '  title:        short text for the entry\n'
        '  datetime:     ISO8601 UTC timestamp (deadline/event-start)\n'
        '  duration_min: integer minutes (only for type==event)\n'
        '  description:  additional details\n'
        "Try to not schedule unimportant things like 'read article' etc. If no scheduling is needed, respond with an empty JSON: {}"
    )
}

def plan_calendar_actions(items: list[dict]) -> list[dict]:
    """
    items each have: {
      'msg_id', 'thread_id', 'subject', 'summary', 'action',
      'importance', 'deep_summary', 'date', 'category'
    }
    Returns a list of tasks WITH msg_id/thread_id added back in:
      { 'msg_id', 'thread_id', 'type', 'title', ... }
    """
    # 1) sort: Important category first, then by importance desc
    sorted_items = sorted(
        items,
        key=lambda x: (0 if x.get("category")=="Important" else 1, -x.get("importance",0))
    )
    total = len(sorted_items)
    scheduled = []

    for i, item in tqdm(enumerate(sorted_items, start=1), desc=f'Planning {total} tasks...'):
        # build the JSON list of already-scheduled tasks (drop IDs)
        so_far = [
            { k:v for k,v in t.items() if k not in ("msg_id","thread_id") }
            for t in scheduled
        ]
        so_far = so_far[:10]
        so_far_json = json.dumps(so_far, indent=2) if so_far else "[]"

        # 2) per-item prompt
        prompt = (
            "/think\n"
            f"You are reviewing {total} email‐derived items. This is item {i}/{total}.\n\n"
            f"Already scheduled tasks so far:\n{so_far_json}\n\n"
            f"Now consider this email:\n"
            f"Subject: {item['title']}\n"
            f"Date: {item['date']}\n"
            f"Summary: {item['description']}\n"
            f"Suggested Action: {item['action']}\n"
            f"Category: {item['category']}\n"
            f"Importance (within-category): {item['importance']}\n"
            f"Details: {item.get('deep_summary','')}\n\n"
            "Decide whether to schedule it.  Respond only with the JSON object described by the system "
            "with fields: [type, title, datetime, duration_min, description]"
        )

        messages = [
            SYSTEM,
            {"role":"user","content":prompt}
        ]

        # 3) call the model
        resp = llama_chat(messages, max_tokens=8192)
        
        if not isinstance(resp, list):
            continue  # skip if empty or malformed
        if len(resp) > 0:
            # 4) stitch back in our hidden IDs
            task = {
                "msg_id":     item["msg_id"],
                "thread_id":  item["thread_id"],
                "acct_email": item["acct_email"],
                **resp[0]
            }
            scheduled.append(task)

    logging.info("plan_calendar_actions: %d tasks scheduled", len(scheduled))
    return scheduled
