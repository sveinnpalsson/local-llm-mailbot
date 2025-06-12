import json
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

from tqdm import tqdm

from .classifier       import llama_chat
from .config_private   import ACCOUNTS, USER_PROFILE_LLM_PROMPT_DEEP
from .db               import (
    cache_raw_message,
    get_all_contacts,
    get_cached_ids,
    get_conn,
    load_raw_message,
    set_contact_profile,
    update_contact,
    get_contact_profile
)
from .gmail_client     import (
    get_service,
    fetch_full_message_payload,
    fetch_message_ids,
    fetch_messages,
    get_full_message_from_payload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")

SYSTEM_PROFILE_PROMPT = (
    "You are a contact‐profiling assistant.  "
    "Given full conversation threads between the user and a contact, "
    "you must produce *only* a JSON object with exactly these fields:\n"
    "  • role: e.g. “colleague”, “friend”, etc.\n"
    "  • common_topics: an array of keywords discussed\n"
    "  • tone: “formal” or “casual”\n"
    "  • relationship: a phrase like “your project manager”\n"
    "  • notes: any other brief, useful observations about this contact\n"
    f" {USER_PROFILE_LLM_PROMPT_DEEP}\n"
    "Do not output any commentary or anything outside the JSON.\n"
)

SYSTEM_PROFILE_UPDATE_PROMPT = (
    "You are a contact‐profiling assistant.  "
    "Given the current profile of a contact and the most recent email received from them, "
    "you must produce *only* a JSON object with an updated profile (same fields as the provided profile):\n"
    "If you believe the current profile does NOT need to be updated, you may output an empty json: \{\}."
    ".\n"
)


def ensure_raw_cached(svc, conn, msg_id):
    """
    Load raw JSON payload from cache or fetch+cache it.
    """
    raw = load_raw_message(conn, msg_id)
    if raw is None:
        # fetch full payload and cache it
        raw = fetch_full_message_payload(svc, msg_id)
        cache_raw_message(conn, msg_id, json.dumps(raw))
    else:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    return raw


def build_profiles(account):
    svc  = get_service(account["credentials_file"], account["token_file"])
    conn = get_conn()
    me   = account["email"]

    # Update contact stats from SENT
    sent_ids = fetch_message_ids(svc, query="label:SENT", max_results=200)
    for mid in tqdm(sent_ids, desc='Looking through SENT'):
        raw = ensure_raw_cached(svc, conn, mid)
        hdrs = {h['name']: h['value'] for h in raw['payload']['headers']}
        dt   = parsedate_to_datetime(hdrs.get('Date'))
        frm  = hdrs.get('From','')
        tos  = [t.strip() for t in hdrs.get('To','').split(',')]
        update_contact(conn, frm, dt)
        for t in tos:
            update_contact(conn, t, dt)

    # Now build profiles for any contact without one
    for email, name, profile_json in tqdm(get_all_contacts(conn), desc='Building profiles'):
        if profile_json:
            continue

        logging.info("Building profile for %s", email)

        # Gather up to 5 full threads with that contact
        threads = svc.users().threads().list(
            userId='me', q=f"label:SENT to:{email}", maxResults=200
        ).execute().get('threads', [])

        convo_texts = []
        for th in threads:
            tid = th['id']
            thread = svc.users().threads().get(
                userId='me', id=tid, format='full'
            ).execute()
            msgs = thread.get('messages', [])

            # Keep only threads you actually participated in
            if not any("SENT" in m.get('labelIds',[]) for m in msgs):
                continue

            # Sort chronologically
            msgs_sorted = sorted(msgs, key=lambda m: int(m['internalDate']))
            parts = []
            for m in msgs_sorted:
                mid = m['id']
                raw = ensure_raw_cached(svc, conn, mid)
                subj, snip, body, _, _, _, _, _ = \
                    get_full_message_from_payload(svc, raw)
                hdrs = {h['name']:h['value'] for h in raw['payload']['headers']}
                role = "You" if me in hdrs.get('From','') else email
                text = (body[:300] + '…') if len(body)>300 else body
                parts.append(f"{role}: {text.replace(chr(10),' ')}")
            if parts:
                convo_texts.append("\n".join(parts))

        if not convo_texts:
            logging.info("No valid SENT threads for %s", email)
            continue

        convo_texts = convo_texts[:5]  # cap at 5 threads #TODO: maybe make a config param
        profile_input = "\n\n---\n\n".join(convo_texts)

        # Build the *messages* array with system + user
        messages = [
            {"role": "system", "content": SYSTEM_PROFILE_PROMPT},
            {"role": "user",   "content":
                "Here are conversation threads:\n\n"
                + profile_input
                + "\n\nRespond ONLY with the JSON object described."
            }
        ]

        # Call the LLM
        prof = llama_chat(messages, max_tokens=8192)[0]
        if isinstance(prof, dict):
            set_contact_profile(conn, email, prof)
            logging.info("Profile set for %s: %s", email, prof)
        else:
            logging.warning("Failed to build profile for %s", email)

def update_contact_profile(conn, email: str, rec: dict) -> dict:
    """Prompt the LLM to update the contact profile for this email."""
    old_profile = get_contact_profile(conn, email) or {}
    old_profile = json.dumps(old_profile)
    if old_profile == '{}': # Treat as a new contact
        messages = [
            {"role": "system", "content": SYSTEM_PROFILE_PROMPT},
            {"role": "user",   "content":
                "No conversation thread exists for this contact but here is an email just received from this contact:\n\n"
                + email
                + "\n\nRespond ONLY with the JSON object described."
            }
        ]
    else: # Update contact
        messages = [
            {"role": "system", "content": SYSTEM_PROFILE_UPDATE_PROMPT},
            {"role": "user",   "content":
                "Here is the newest email:\n\n"
                + email
                + "\n\nContact's current profile:\n\n"
                + old_profile
            }
        ]

    result = llama_chat(messages, max_tokens=8192)[0]

    if result and result != '{}':
        updated_profile = result
        return updated_profile
    return None


if __name__ == "__main__":
    from .gmail_client import ensure_tokens
    logging.info("Authenticating")
    if not ensure_tokens():
        raise
    
    for acct in ACCOUNTS:
        logging.info(f"Running profile builder for account: {acct['email']}")
        try:
            build_profiles(acct)
        except Exception:
            logging.exception("Error building profiles for %s", acct["name"])
