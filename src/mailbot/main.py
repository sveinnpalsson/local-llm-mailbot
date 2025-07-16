import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError
from tqdm import tqdm

from .classifier         import deep_analyze, initial_classify
from .config             import (
    DEEP_THRESHOLD_IMPORTANCE,
    DB_PASSWORD,
    NUM_MESSAGES_LOOKBACK,
    MIN_IMPORTANCE_FOR_ALERT,
    PLANNING_INTERVAL_HOURS,
    POLL_INTERVAL_SECONDS,
)
from .config_private     import ACCOUNTS
from .db                 import (
    add_task,
    cache_raw_message,
    get_contact_profile,
    get_conn,
    load_raw_message,
    mark_email,
    mark_task_sent,
    set_contact_profile
)
from .gmail_client       import (
    fetch_full_message_payload,
    get_full_message_from_payload,
    get_service,
    ensure_tokens,
    fetch_history_with_retry
)
from .telegram_message   import send_telegram
from .profile_builder    import update_contact_profile
from .task_agents import handle_action
from .telegram_listener import start_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# Number of messages per LLM batch
BATCH_SIZE = 10
N_MAX = 20
SEND_TELEGRAM_NOTIFICATIONS = False #TODO: Implement with end-to-end encryption
update_profiles = True 
SPAMMERS: dict[str, set[str]] = {
    acct["email"]: set()
    for acct in ACCOUNTS
}

def process_message(svc, conn, acct, mid, spammers):
    """
    Fetch, parse, classify & store one message, preserving your debug prints.
    """
    # Raw payload
    raw = load_raw_message(conn, mid)
    if raw is None:
        raw = fetch_full_message_payload(svc, mid)
        if raw is None:
            return None   # simply skip this message
        cache_raw_message(conn, mid, json.dumps(raw))

    # peek headers for spam skip
    hdrs      = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
    from_addr = hdrs.get("From","")
    # we haven't parsed date_iso yet, so skip the spam print until after parsing

    # full parse
    subject, snippet, body, thread_id, frm, to_addr, date_iso, msg_dt = \
        get_full_message_from_payload(svc, raw)

    # now we know date_iso—spam skip print:
    if frm in spammers:
        print(f'// SPAM // {date_iso} FROM: {frm} SUBJECT: {subject}')
        return None

    # shallow classify
    init = initial_classify(subject, snippet, frm, to_addr, date_iso, msg_dt)

    # if Spam, record & add to spammers
    if init.get("category") == "Spam":
        spammers.add(frm)
        print(f'// SPAM // {date_iso} FROM: {frm}')
        rec = {
            "msg_id":      mid,
            "from":        frm,
            "to":          to_addr,
            "thread_id":   thread_id,
            "subject":     subject,
            "snippet":     snippet,
            "date":        date_iso,
            **init,
            "deep_summary":""
        }
        mark_email(conn, rec)
        return rec

    # build record
    rec = {
        "msg_id":      mid,
        "from":        frm,
        "to":          to_addr,
        "thread_id":   thread_id,
        "subject":     subject,
        "snippet":     snippet,
        "date":        date_iso,
        **init,
        "deep_summary":""
    }

    # deep if flagged
    if init["importance"] >= DEEP_THRESHOLD_IMPORTANCE \
       or init["category"] == "Important":

        prof_from = get_contact_profile(conn, frm)
        prof_to   = get_contact_profile(conn, to_addr)
        deep = deep_analyze(
            subject, body, frm, to_addr,
            date_iso, msg_dt,
            init["category"], init["importance"],
            init["action"], init["summary"],
            contact_profile_sender=prof_from,
            contact_profile_recipient=prof_to
        )
        rec.update(deep)

    # Printing
    print('------------------------------------------------------')
    print(f'{date_iso} FROM: {frm} Subject: {subject}')
    print(f'Category: {rec["category"]} -- Importance: {rec["importance"]} -- Action: {rec["action"]}')
    summary_to_print = rec["deep_summary"] if rec["deep_summary"] else rec["summary"]
    print(f'Summary: {summary_to_print}')
    print('------------------------------------------------------')
    
    rec["agent_output"] = ""
    mark_email(conn, rec)

    # Run agent to handle actions
    agent_result = handle_action(rec)
    rec["agent_output"] = agent_result or ""


    # Update contact profile
    if update_profiles:
        updated_profile = update_contact_profile(conn, frm, rec)
        if updated_profile:
            set_contact_profile(conn, frm, updated_profile)
            print(f"UPDATED PROFILE FOR: {frm}")
        
    # write to database
    mark_email(conn, rec)

    # alert if needed
    if rec["importance"] >= acct.get("min_alert", MIN_IMPORTANCE_FOR_ALERT) \
        or rec["category"] == "Important":

        gmail_link = f"https://mail.google.com/mail/u/0/#all/{rec['thread_id']}"

        msg = (
            f"📧 *New {rec['category']} Email*\n"
            f"*Subject:* {rec['subject']}\n"
            f"*Importance:* {rec['importance']}\n"
            f"*Action:* {rec['action']}\n"
            f"*Summary:* {rec['summary']}\n"
        )
        if rec.get("deep_summary"):
            msg += f"*Details:* {rec['deep_summary']}\n"

        msg += f"[Open in Gmail]({gmail_link})"

        if SEND_TELEGRAM_NOTIFICATIONS:
            send_telegram(msg)
    return rec

def main_loop():
    conn = get_conn()
    services    = {}
    history_ids = {}
    spammers    = {acct["email"]: set() for acct in ACCOUNTS}

    # Build service clients & backfill recent messages
    for acct in ACCOUNTS:
        email = acct["email"]
        print(f"Setting up gmail service for {email}")
        svc   = get_service(acct["credentials_file"], acct["token_file"])
        services[email] = svc

        # Pull the last N message‐IDs
        resp   = svc.users().messages().list(
            userId='me',
            labelIds=['INBOX'],
            maxResults=NUM_MESSAGES_LOOKBACK
        ).execute()
        recent = resp.get("messages", [])
        mids   = [m["id"] for m in recent]

        # Find which of those are already in our emails table
        if mids:
            placeholders = ",".join("?" for _ in mids)
            seen_rows = conn.execute(
                f"SELECT msg_id FROM emails WHERE msg_id IN ({placeholders})",
                mids
            ).fetchall()
            seen = {r[0] for r in seen_rows}
        else:
            seen = set()

        # The ones we actually need to process
        new_mids = [mid for mid in mids if mid not in seen]

        # Log the summary before entering the loop
        logging.info(
            "Backfilling last %d messages for %s: %d new to process",
            NUM_MESSAGES_LOOKBACK,
            email,
            len(new_mids),
        )

        # Process only the truly new ones
        for mid in new_mids:
            logging.info("Processing historic msg %s for %s", mid, email)
            try:
                process_message(svc, conn, acct, mid, spammers[email])
            except Exception as e:
                logging.exception("Error backfilling msg %s: %s", mid, e)

        # After backfill, initialize your history cursor to the mailbox tip
        profile = svc.users().getProfile(userId='me').execute()
        history_ids[email] = int(profile["historyId"])
        logging.info("Initialized historyId for %s → %s", email, history_ids[email])

    logging.info("Startup backfill complete; entering continuous listener (poll every %ds)…",
                 POLL_INTERVAL_SECONDS)

    # Poll‐loop for truly new mail
    while True:
        for acct in ACCOUNTS:
            email    = acct["email"]
            svc      = services[email]
            start_id = history_ids[email]

            try:
                logging.info("Checking Gmail history for %s (since %s)", email, start_id)
                resp = fetch_history_with_retry(
                    svc,
                    userId='me',
                    startHistoryId=start_id,
                    historyTypes=['messageAdded'],
                    labelId='INBOX'
                )
            except HttpError as e:
                logging.error("Gmail history error for %s: %s", email, e)
                continue

            records = resp.get("history", [])
            if not records:
                logging.info("No new INBOX messages for %s", email)
            else:
                # process each truly new message
                for record in records:
                    for added in record.get("messagesAdded", []):
                        mid = added["message"]["id"]
                        logging.info("New msg %s detected for %s", mid, email)
                        try:
                            process_message(svc, conn, acct, mid, spammers[email])
                        except Exception as e:
                            logging.exception("Error processing msg %s: %s", mid, e)

                # advance the cursor once done
                new_hist = int(resp.get("historyId", start_id))
                if new_hist != start_id:
                    history_ids[email] = new_hist
                    logging.info("Advanced cursor for %s → %s", email, new_hist)

        logging.info("Sleeping for %d seconds…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    ensure_tokens()
    start_listener()
    main_loop()