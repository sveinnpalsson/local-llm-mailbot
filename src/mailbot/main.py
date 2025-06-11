import logging
from datetime import timedelta
from datetime import datetime, date
from collections import defaultdict
from classifier   import initial_classify, deep_analyze
from db import (
    get_conn, get_seen_ids,
    get_cached_ids, cache_raw_message, load_raw_message, mark_email, get_contact_profile,
    reset_emails_table, reset_tasks_table, add_task, mark_task_sent, get_due_tasks
)
from gmail_client import (
    get_service, fetch_message_ids,
    fetch_full_message_payload, get_full_message_from_payload
)
from googleapiclient.errors import HttpError

from telegram_message        import send_telegram
from config            import (
    ACCOUNTS,
    DEEP_THRESHOLD_IMPORTANCE,
    MIN_IMPORTANCE_FOR_ALERT,
    LOOKBACK_WEEKS,
    CALENDAR_IMPORTANCE_THRESHOLD,
    POLL_INTERVAL_SECONDS,
    PLANNING_INTERVAL_HOURS
)
import os
import json
from tqdm import tqdm
from calendar_client   import get_calendar_service, create_calendar_event
from calendar_planner  import plan_calendar_actions
from config            import LOOKBACK_WEEKS, DB_PASSWORD
from config_private import ACCOUNTS
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")

# ——— Configuration ———

# Number of messages per LLM batch
BATCH_SIZE = 10
N_MAX = 20
SEND_TELEGRAM_NOTIFICATIONS = False #TODO: Implement with end-to-end encryption

def ensure_tokens() -> bool:
    """
    For each account in ACCOUNTS, if its token file doesn't exist,
    run the OAuth flow to create it, then tell the user to re-run.
    Returns True if all tokens already existed, False if new ones were made.
    """
    missing = []
    for acct in ACCOUNTS:
        if not os.path.exists(acct["token_file"]):
            missing.append(acct)

    if not missing:
        return True

    for acct in missing:
        email = acct["email"]
        logging.info("→ Generating OAuth token for %s …", email)
        # This call will open your browser (or console) to complete the OAuth flow
        get_service(acct["credentials_file"], acct["token_file"])
        logging.info("✓ Token saved to %s", acct["token_file"])

    print(f"\nCreated {len(missing)} new token file(s).")
    print("Please re-run this script now that all tokens exist.")
    return False

def run_due_reminders(conn):
    now = datetime.now()
    rows = conn.execute("""
      SELECT t.task_id, t.type, t.title, t.target_date, e.thread_id, t.acct_email
        FROM tasks t
        JOIN emails e ON t.msg_id = e.msg_id
       WHERE t.sent = 0
         AND t.scheduled_time <= ?
    """, (now.isoformat(),)).fetchall()

    if not rows:
        logging.info("No due reminders at %s", now)
        return

    for task_id, kind, title, target, thread_id, acct_email in rows:
        # UNIVERSAL LINK for Gmail
        gmail_link = (
            f"https://mail.google.com/mail/"
            f"?authuser={acct_email}"
            f"#all/{thread_id}"
        )

        ios_link = f"googlegmail:///thread?th={thread_id}"

        if kind == 'reminder':
            msg = (
                "⏰ *Reminder Scheduled*\n"
                f"*{title}*\n"
                f"On: {target}\n\n"
                f"[Open in Gmail]({gmail_link})"
            )
            send_telegram(msg, html=False)  # Markdown mode
        else:
            msg = (
                "📅 *Upcoming Event*\n"
                f"*{title}*\n"
                f"Scheduled at: {target}\n\n"
                f"[Open in Gmail]({gmail_link})"
            )
            send_telegram(msg, html=False)

        mark_task_sent(conn, task_id)
        logging.info("Sent reminder for task %d (thread %s)", task_id, thread_id)



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
        print(f'// SPAM // {date_iso} FROM: {frm}')
        return None

    # update contacts #TODO: needs implementation - want to update contact info on the fly
    # if msg_dt:
    #     update_contact(conn, frm, msg_dt)
    #     update_contact(conn, to_addr, msg_dt)

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


def schedule_calendar_stage(conn, account_address=None, max_days=7):
    # Prepare Calendar API client (always first account)
    cal_acct = ACCOUNTS[0]
    cal_svc  = get_calendar_service(
      cal_acct["calendar_credentials_file"],
      cal_acct["calendar_token_file"]
    )

    # Fetch candidate emails
    cutoff = datetime.now() - timedelta(days=max_days)
    cutoff_date = cutoff.date().isoformat()

    sql = """
      SELECT
        e.msg_id,
        e.thread_id,
        e.subject,
        e.summary,
        e.action,
        e.importance,
        e.deep_summary,
        e.category,
        e.date
      FROM emails e
      WHERE (e.category = 'Important' OR e.importance >= ?)
        AND e.date >= ?
    """
    params = [CALENDAR_IMPORTANCE_THRESHOLD, cutoff_date]
    if account_address:
        sql += " AND (e.from_addr = ? OR e.to_addr = ?)"
        params += [account_address, account_address]

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        logging.info("No calendar-worthy emails in the last %d days%s",
                     max_days,
                     f" for {account_address}" if account_address else "")
        return
    
    processed = {
        row[0]
        for row in conn.execute("SELECT DISTINCT msg_id FROM tasks").fetchall()
    }
    # Plan tasks via LLM
    items = []
    for msg_id, thread_id, subj, summ, act, imp, deep, cat, date_iso in rows:
        if msg_id in processed:
            continue
        items.append({
            "msg_id":        msg_id,
            "thread_id":     thread_id,
            "title":         subj,         # what the planner shows as the title
            "description":   summ,         # what the planner shows as the body/summary
            "action":        act,          # optional hint (if you still want it)
            "importance":    imp,          # used for sorting
            "deep_summary":  deep or "",   # used for decision‐making
            "category":      cat,          # to force “Important” first
            "date":          date_iso,     # for reminder scheduling
            "acct_email":    account_address
        })
    tasks = plan_calendar_actions(items)
    if not tasks:
        logging.info("Planner returned no tasks")
        return
    
    # For each planned task, create or schedule
    for t in tasks:
        thread_id = t["thread_id"]
        # universal Gmail link (https → opens Gmail app on iOS)
        if account_address:
            gmail_link = (
                f"https://mail.google.com/mail/"
                f"?authuser={account_address}"
                f"#all/{thread_id}"
            )
        else:
            gmail_link = f"https://mail.google.com/mail/u/0/#all/{thread_id}"


        if "type" not in t:
            logging.warning("Skipping task without type: %s", t)
            continue

        if t["type"] == "event":
            # parse times
            dt_start = datetime.fromisoformat(t["datetime"])
            dt_end   = dt_start + timedelta(minutes=t.get("duration_min", 30))

            # only insert once
            inserted = add_task(
                conn,
                t["msg_id"],
                'event',
                t["title"],
                t["datetime"],
                account_address,
                dt_start - timedelta(days=1)
            )
            if not inserted:
                continue

            # create the Calendar event
            ev      = create_calendar_event(
                cal_svc,
                summary=t["title"],
                description=t["description"],
                start_dt=dt_start,
                end_dt=dt_end
            )
            cal_web = ev.get("htmlLink", "")

            # build HTML message
            msg = (
                "📅 <b>Event Added to Google Calendar</b>\n"
                f"<b>{t['title']}</b>\n"
                f"{dt_start:%Y-%m-%d %H:%M} for "
                f"{(dt_end - dt_start).seconds//60}m\n\n"
                "<b>Calendar:</b> "
                f'<a href="{cal_web}">Web</a>\n'
                "<b>Email:</b> "
                f'<a href="{gmail_link}">Open in Gmail</a>'
            )
            send_telegram(msg, html=True)

        else:  # reminder
            # extract pure date
            date_str  = t["datetime"].split("T", 1)[0]
            dt_target = datetime.fromisoformat(date_str)
            sched     = dt_target - timedelta(days=2)

            inserted = add_task(
                conn,
                t["msg_id"],
                'reminder',
                t["title"],
                t["datetime"],
                account_address,
                sched
            )
            if not inserted:
                continue

            msg = (
                "⏰ <b>Reminder Scheduled</b>\n"
                f"<b>{t['title']}</b>\n"
                f"On: {date_str}\n\n"
                "<b>Email:</b> "
                f'<a href="{gmail_link}">Open in Gmail</a>'
            )
            send_telegram(msg, html=True)

    logging.info("Calendar stage complete; tasks upserted via %s", cal_acct["email"])


def send_daily_digest():
    conn = get_conn()
    today_iso = date.today().isoformat()

    # Fetch today's processed emails
    rows = conn.execute("""
        SELECT subject, category, importance, action, summary, deep_summary
          FROM emails
         WHERE date(processed_at) = ?
    """, (today_iso,)).fetchall()

    if not rows:
        logging.info("No emails processed today; skipping daily digest.")
        return

    # Group by category
    by_cat = defaultdict(list)
    for subj, cat, imp, action, summ, deep in rows:
        by_cat[cat].append({
            "subject": subj,
            "importance": imp,
            "action": action,
            "summary": summ,
            "deep_summary": deep or ""
        })

    # Build the digest text
    lines = [f"📬 *Daily Digest for {today_iso}*"]
    for cat, items in by_cat.items():
        lines.append(f"\n*{cat}*:")
        # top 5 by importance
        top5 = sorted(items, key=lambda x: -x["importance"])[:5]
        for it in top5:
            lines.append(
                f"• *{it['action']}* _(Importance {it['importance']})_\n"
                f"    – Summary: {it['summary']}"
            )
            if it["deep_summary"]:
                lines.append(f"    – Details: {it['deep_summary']}")
    message = "\n".join(lines)

    # Send via Telegram
    #send_telegram("📅 Today's Email Digest", message)
    logging.info("Sent daily digest with %d categories", len(by_cat))




def main_loop():
    conn = get_conn()

    # Prebuild a service + historyId for each account,
    #    resuming from the last processed message if possible.
    services    = {}
    history_ids = {}

    for acct in ACCOUNTS:
        email = acct["email"]
        print(f"Initializing account: {email}")
        svc = get_service(acct["credentials_file"], acct["token_file"])
        services[email] = svc

        # Try to load the historyId from the most recently processed message:
        row = conn.execute("""
            SELECT rm.raw_json
              FROM raw_messages rm
              JOIN emails e ON rm.msg_id = e.msg_id
             WHERE e.to_addr = ?
             ORDER BY e.processed_at DESC
             LIMIT 1
        """, (email,)).fetchone()

        last_hist = None
        if row:
            try:
                data = json.loads(row[0])
                last_hist = int(data.get("historyId"))
                logging.info(
                    "Loaded historyId %s for %s from last processed message",
                    last_hist, email
                )
            except Exception:
                logging.warning(
                    "Failed to parse historyId from raw JSON for %s; will fetch fresh",
                    email
                )

        if not last_hist:
            # No prior data → fall back to current mailbox tip
            profile = svc.users().getProfile(userId='me').execute()
            last_hist = int(profile["historyId"])
            logging.info(
                "No prior historyId for %s—initialized to tip %s",
                email, last_hist
            )

        history_ids[email] = last_hist

    # Schedule planning for the very first time as "just now"
    last_planning = datetime.now() - timedelta(hours=PLANNING_INTERVAL_HOURS)
    logging.info(
        "Starting continuous listener (poll every %ds)...",
        POLL_INTERVAL_SECONDS
    )

    # Enter the poll‐loop
    while True:
        now = datetime.now()
        logging.info("=== Loop at %s ===", now.strftime("%Y-%m-%d %H:%M:%S"))
        any_important = False

        # Check each account via history.list
        for acct in ACCOUNTS:
            email    = acct["email"]
            svc      = services[email]
            start_id = history_ids[email]

            logging.info("Checking Gmail history for %s (since %s)", email, start_id)
            try:
                resp = svc.users().history().list(
                    userId='me',
                    startHistoryId=start_id,
                    historyTypes=['messageAdded'],
                    labelId='INBOX'
                ).execute()
            except HttpError as e:
                logging.error("Gmail history error for %s: %s", email, e)
                continue

            # Update to the new historyId (even if no new messages)
            new_hist = int(resp.get("historyId", start_id))
            history_ids[email] = new_hist
            logging.info("Updated historyId for %s → %s", email, new_hist)

            records = resp.get("history", [])
            if not records:
                logging.info("No new INBOX messages for %s", email)
                continue

            # Process each new messageAdded
            for record in records:
                for added in record.get("messagesAdded", []):
                    mid = added["message"]["id"]
                    logging.info("New msg %s detected for %s", mid, email)
                    rec = process_message(svc, conn, acct, mid, spammers=set())
                    if rec:
                        logging.info(
                            "Processed msg %s: category=%s importance=%s",
                            mid, rec["category"], rec["importance"]
                        )
                        if (rec["category"] == "Important"
                            or rec["importance"] >= acct.get("min_alert", 0)):
                            any_important = True

        # Immediate planning if anything important arrived
        if any_important:
            logging.info("Important email(s) found—running calendar planning now")
            schedule_calendar_stage(conn)
        else:
            logging.info("No important email this cycle")

        # Always run due reminders
        logging.info("Running due reminders")
        run_due_reminders(conn)

        # Periodic full planning every N hours
        if (now - last_planning).total_seconds() >= PLANNING_INTERVAL_HOURS * 3600:
            logging.info("Periodic planning interval reached—running calendar planning")
            schedule_calendar_stage(conn)
            last_planning = now

        # Sleep until next poll
        logging.info("Sleeping for %d seconds...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    ensure_tokens()
    main_loop()