import os
from sqlcipher3 import dbapi2 as sqlite
from .config import DB_PATH, DB_PASSWORD
from datetime import datetime
import json

def get_conn():
    # Open (and decrypt) the database file
    conn = sqlite.connect(DB_PATH, 
        timeout=30.0,            # wait up to 30s for any lock
        check_same_thread=False, # allow multiple threads
    )
    conn.execute(f"PRAGMA key='{DB_PASSWORD}';")
    # Ensure all tables exist (won't overwrite existing ones)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS emails (
      msg_id        TEXT PRIMARY KEY,
      date          TEXT,
      from_addr     TEXT,
      to_addr       TEXT,
      thread_id     TEXT,
      subject       TEXT,
      snippet       TEXT,
      category      TEXT,
      importance    INTEGER,
      action        TEXT,
      summary       TEXT,
      deep_summary  TEXT,
      processed_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS contacts (
      email         TEXT PRIMARY KEY,
      name          TEXT,
      first_seen    DATETIME,
      last_seen     DATETIME,
      message_count INTEGER DEFAULT 0,
      profile_json  TEXT
    );

    CREATE TABLE IF NOT EXISTS ignore_rules (
      rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
      pattern TEXT             -- e.g. a sender email or regex
    );
    CREATE TABLE IF NOT EXISTS raw_messages (
      msg_id     TEXT PRIMARY KEY,
      raw_json   TEXT NOT NULL,
      fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    return conn

def get_cached_ids(conn):
    """Return set of msg_ids we already have stored in raw_messages."""
    cur = conn.execute("SELECT msg_id FROM raw_messages")
    return {row[0] for row in cur}

def cache_raw_message(conn, msg_id: str, raw_json: str):
    """Insert the full JSON payload for msg_id into raw_messages."""
    conn.execute("""
      INSERT OR REPLACE INTO raw_messages (msg_id, raw_json)
      VALUES (?, ?)
    """, (msg_id, raw_json))
    conn.commit()

def get_message_history(
    conn,
    thread_id: str,
    limit: int = 5,
    exclude_msg_id: str | None = None
) -> list[tuple[str, str]]:
    """
    Fetch the most recent `limit+1` messages in the given thread (by date desc),
    then drop `exclude_msg_id` if present, and return up to `limit` entries as
    (date, snippet) tuples.
    """
    cur = conn.execute(
        """
        SELECT msg_id, date, snippet
          FROM emails
         WHERE thread_id = ?
         ORDER BY date DESC
         LIMIT ?
        """,
        (thread_id, limit + (1 if exclude_msg_id else 0),)
    )
    rows = cur.fetchall()
    # filter out the current message itself
    filtered = [
        (date, snippet)
        for (mid, date, snippet) in rows
        if mid != exclude_msg_id
    ]
    # trim to requested limit
    return filtered[:limit]


def get_ignore_rules(conn):
    return [r[0] for r in conn.execute("SELECT pattern FROM ignore_rules")]


def load_raw_message(conn, msg_id: str) -> dict | None:
    """Load a cached raw_messages[msg_id] and parse it back to a dict."""
    cur = conn.execute(
      "SELECT raw_json FROM raw_messages WHERE msg_id = ?",
      (msg_id,)
    )
    row = cur.fetchone()
    return json.loads(row[0]) if row else None

def update_contact(conn, email: str, seen_at: datetime, name: str = None):
    """
    Record that we've seen 'email' at datetime 'seen_at'.
    If the contact is new, insert with 'name' or fallback to the email itself.
    Otherwise, bump its message_count and update last_seen.
    """
    cur = conn.execute("SELECT 1 FROM contacts WHERE email = ?", (email,))
    if cur.fetchone():
        conn.execute(
            "UPDATE contacts "
            "   SET last_seen     = ?,"
            "       message_count = message_count + 1 "
            " WHERE email = ?",
            (seen_at, email)
        )
    else:
        conn.execute(
            "INSERT INTO contacts "
            "(email, name, first_seen, last_seen, message_count, profile_json) "
            "VALUES (?, ?, ?, ?, 1, NULL)",
            (email, name or email, seen_at, seen_at)
        )
    conn.commit()

def get_contact_profile(conn, email: str) -> dict:
    """
    Fetch and parse the JSON profile for a given contact email.
    Returns {} if none is set or on parse errors.
    """
    cur = conn.execute(
        "SELECT profile_json FROM contacts WHERE email = ?",
        (email,)
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}
    
    
def get_all_contacts(conn):
    return conn.execute("SELECT email, name, profile_json FROM contacts").fetchall()

def set_contact_profile(conn, email: str, profile: dict):
    conn.execute("""
      UPDATE contacts SET profile_json = ? WHERE email = ?
    """, (json.dumps(profile), email))
    conn.commit()


def mark_email(conn, rec):
    conn.execute("""
      INSERT OR REPLACE INTO emails
      (msg_id, date, from_addr, to_addr, thread_id,
       subject, snippet, category, importance,
       action, summary, deep_summary, agent_output)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
      rec["msg_id"], rec["date"], rec["from"], rec["to"], rec["thread_id"],
      rec["subject"], rec["snippet"],
      rec["category"], rec["importance"],
      rec["action"], rec["summary"],
      rec.get("deep_summary",""),
      rec.get("agent_output","")
    ))
    conn.commit()

def get_seen_ids(conn):
    cur = conn.execute("SELECT msg_id FROM emails")
    return {r[0] for r in cur}

def reset_emails_table():
    conn = get_conn()
    cur = conn.execute("DROP TABLE IF EXISTS emails;")
    conn.commit()
    conn.close()

def fetch_today(conn, acct=None):
    if acct:
        cur = conn.execute("""
          SELECT subject, category, importance, action, summary
          FROM emails
          WHERE date(processed_at) = date('now', 'localtime')
        """)
        return [
          {"subject":s,"category":c,"importance":i,"action":a,"summary":su}
          for s,c,i,a,su in cur
        ]
    else:
        cur = conn.execute("""
          SELECT subject, category, importance, action, summary
          FROM emails
          WHERE date(processed_at) = date('now', 'localtime')
        """)
        return [
          {"subject":s,"category":c,"importance":i,"action":a,"summary":su}
          for s,c,i,a,su in cur
        ]
