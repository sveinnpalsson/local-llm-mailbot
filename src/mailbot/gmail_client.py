import os
import base64
import fitz
import pwd
import resource

from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from google.oauth2.credentials  import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow  import InstalledAppFlow
from google.auth.exceptions     import RefreshError
from google.auth.exceptions   import TransportError
from googleapiclient.errors   import HttpError

import time
import logging
import ssl
import http.client
from typing            import Tuple, Optional, Dict, Any, List
from datetime import datetime
import multiprocessing
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr
from email.header import decode_header, make_header

from .config_private import ACCOUNTS

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.modify']
SCOPES_CALENDAR = ['https://www.googleapis.com/auth/calendar.events']

def get_service(credentials_file: str, token_file: str):
    creds = None
    # 1) Load existing token if it exists
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            safe_refresh(creds)
            with open(token_file, 'w') as f:
                f.write(creds.to_json())
        except Exception:       # catches TransportError, SSL errors, etc.
            os.remove(token_file)
            creds = None
            
    # 3) If no valid creds, run the OAuth flow
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_file,
            SCOPES
        )
        creds = flow.run_local_server(
            port=0,
            access_type='offline',
            prompt='consent'
        )
        with open(token_file, 'w') as f:
            f.write(creds.to_json())

    # 4) Build the Gmail API client
    return build('gmail', 'v1', credentials=creds)


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
    
    
    if ACCOUNTS[0]["calendar_credentials_file"] != "" and not os.path.exists(ACCOUNTS[0]["calendar_token_file"]):
        logging.info("→ Generating Calendar OAuth token for ...%s ", email)
        get_calendar_service(
            ACCOUNTS[0]["calendar_credentials_file"],
            ACCOUNTS[0]["calendar_token_file"]
        )

    if not missing:
        return True

    for acct in missing:
        email = acct["email"]
        logging.info("→ Generating OAuth token for %s ...", email)
        # This call will open your browser (or console) to complete the OAuth flow
        get_service(acct["credentials_file"], acct["token_file"])
        logging.info("✓ Token saved to %s", acct["token_file"])
    
    print(f"\nCreated {len(missing)} new token file(s).")
    print("Please re-run this script now that all tokens exist.")
    return False


def fetch_history_with_retry(svc, **kwargs):
    delay = 1
    for attempt in range(5):  # e.g. up to 5 retries
        try:
            return svc.users().history().list(**kwargs).execute()
        except HttpError as e:
            status = getattr(e, 'status_code', None) or e.resp.status
            if status == 503:
                logging.warning("Gmail 503 backendError; retrying in %ds…", delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    raise RuntimeError("Exceeded retries fetching Gmail history")

def safe_execute(callable_execute, retries: int = 3, backoff: float = 1.0):
    """
    Calls `callable_execute()`, which should return an object with .execute().
    Retries up to `retries` times on TransportError, HttpError 5xx,
    RemoteDisconnected, or SSLEOFError, with exponential backoff.
    """
    for attempt in range(1, retries + 1):
        try:
            return callable_execute().execute()
        except (TransportError, ssl.SSLEOFError, http.client.RemoteDisconnected) as e:
            if attempt == retries:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))
        except HttpError as e:
            # retry 5xx server errors
            if 500 <= e.status_code < 600 and attempt < retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            raise

def _walk_parts(
    parts: List[Dict],
    service,
    msg_id: str,
    collected: Dict[str, List],
    allowed_attachments: List[str] = None
):
    """
    Recursively walk MIME parts, appending text to collected['plain']
    or collected['html'], and attachments (by filename) into collected['pdfs'].
    Only attachments whose filenames are in allowed_attachments will be fetched.
    """
    MAX_PART_BYTES = 5 * 1024 * 1024  # 5 MiB per part
    allowed_attachments = allowed_attachments or []

    for part in parts:
        mime = part.get('mimeType', '')
        fn   = (part.get('filename') or "").lower()
        body = part.get('body', {})
        data = body.get('data')

        # 1) TEXT parts
        if mime in ("text/plain", "text/html") and data:
            raw = base64.urlsafe_b64decode(data)
            if len(raw) <= MAX_PART_BYTES:
                text = raw.decode('utf-8', errors='ignore')
                key  = 'plain' if mime == "text/plain" else 'html'
                collected[key] += text + "\n"

        # 2) PDF attachments (only if listed in allowed_attachments)
        elif fn.endswith('.pdf') and fn in allowed_attachments and 'attachmentId' in body:
            att = safe_execute(lambda:
                service.users()
                       .messages()
                       .attachments()
                       .get(userId='me', messageId=msg_id, id=body["attachmentId"])
            )
            raw = base64.urlsafe_b64decode(att.get('data', ''))
            if len(raw) <= MAX_PART_BYTES:
                collected['pdfs'].append(raw)

        # 3) Recurse into nested parts
        if 'parts' in part:
            _walk_parts(part['parts'], service, msg_id, collected, allowed_attachments)


def safe_refresh(creds, request=None, retries: int = 3, backoff: float = 1.0):
    """
    Refreshes credentials, retrying on network/SSL blips up to `retries` times.
    """
    if request is None:
        request = Request()

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(request)
            return
        except (TransportError, ssl.SSLEOFError, http.client.RemoteDisconnected) as e:
            if attempt == retries:
                # no more retries left: re-raise
                raise
            # otherwise sleep & retry
            time.sleep(backoff * (2 ** (attempt - 1)))
            
def fetch_messages(service, query='label:INBOX', max_results=100):
    resp = service.users().messages().list(
      userId='me', q=query, maxResults=max_results
    ).execute()
    return resp.get('messages',[])


def fetch_message_ids(service, query: str = 'label:INBOX', max_results: int = 200):
    """List only message IDs under the given query."""
    resp = service.users().messages().list(
      userId='me', q=query, maxResults=max_results
    ).execute()
    return [m['id'] for m in resp.get('messages', [])]


def fetch_full_message_payload(service, msg_id):
    """
    Returns the message payload (dict) or None if it’s been deleted/missing.
    """
    try:
        return safe_execute(lambda: service.users()
                                         .messages()
                                         .get(userId='me',
                                              id=msg_id,
                                              format='full'))
    except HttpError as e:
        if e.resp.status == 404:
            logging.warning("Gmail message %s not found (404); skipping", msg_id)
            return None
        # re-raise any other errors
        raise


def _pdf_worker(pdf_bytes):
    # 1) Drop to nobody:nogroup
    nobody = pwd.getpwnam("nobody")
    os.setgid(nobody.pw_gid)
    os.setuid(nobody.pw_uid)

    # 2) Enforce resource limits
    resource.setrlimit(resource.RLIMIT_AS, (100*1024*1024, 100*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))

    # 3) Now parse
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "\n".join(page.get_text() for page in doc)

def decode_name(name: str) -> str:
    # Handles =?utf-8?B?...?= etc.
    try:
        return str(make_header(decode_header(name))).strip()
    except Exception:
        return name.strip()

def parse_address_header(value: str) -> list[tuple[str, str]]:
    """
    Returns [(name, email), ...] for a header value that may contain 0..N addresses.
    Name is decoded, email is lowercased.
    """
    pairs = []
    for name, email in getaddresses([value or ""]):
        if not email:
            continue
        pairs.append((decode_name(name), email.strip().lower()))
    return pairs

def get_full_message_from_payload(
    service,
    raw: Dict,
    load_attachments: bool = False,
    allowed_attachments: List[str] = None
) -> Tuple[
    str,    # subject
    str,    # snippet
    str,    # full body text
    str,    # thread_id
    str,    # from_addr_raw
    str,    # from_addr
    str,    # to_addr
    Optional[str],  # date_iso
    Optional[datetime],  # msg_dt
    str,    # ubsub_link
]:
    """
    Extracts subject, snippet, full text (with optional PDF attachments), thread ID,
    from/to addresses, ISO date, and datetime from a raw Gmail message payload.
    """
    from email.utils import parsedate_to_datetime

    # 1) Headers
    headers = {h['name']: h['value'] for h in raw['payload']['headers']}
    subject   = headers.get('Subject', '(no subject)')
    thread_id = raw.get('threadId', '')
    from_addr_raw = headers.get('From', '')
    to_addr_raw   = headers.get('To', '')

    from_list = parse_address_header(from_addr_raw)
    to_list   = parse_address_header(to_addr_raw)

    from_name, from_addr = (from_list[0] if from_list else ("", from_addr_raw))
    to_name,   to_addr   = (to_list[0]   if to_list   else ("", to_addr_raw))

    unsub_link = headers.get('List-Unsubscribe', '')

    # 2) Date parsing
    date_hdr = headers.get('Date')
    try:
        msg_dt   = parsedate_to_datetime(date_hdr)
        date_iso = msg_dt.date().isoformat()
    except Exception:
        msg_dt   = None
        date_iso = None

    # 3) Walk MIME parts
    collected = {'plain': '', 'html': '', 'pdfs': []}
    payload = raw.get('payload', {})
    parts = payload.get('parts', [payload])

    # Only fetch attachments if requested
    _walk_parts(
        parts,
        service,
        raw.get('id', ''),
        collected,
        allowed_attachments if load_attachments else []
    )

    # 4) Choose plain text or fallback to stripped HTML
    if collected['plain'].strip():
        body = collected['plain']
    elif collected['html'].strip():
        body = BeautifulSoup(collected['html'], 'html.parser').get_text(separator='\n')
    else:
        body = ''

    # 5) Append PDF text (if any), sandbox responsibly
    for pdf_bytes in collected['pdfs']:
        try:
            text = extract_pdf_text_sandboxed(pdf_bytes)
            body += "\n" + text
        except Exception:
            continue

    # 6) Snippet (first 200 chars, single-line)
    snippet = (body[:200] + '…') if len(body) > 200 else body
    snippet = snippet.replace('\n', ' ')

    return subject, snippet, body, thread_id, from_addr_raw, from_addr, to_addr, date_iso, msg_dt, unsub_link

def extract_pdf_text_sandboxed(pdf_bytes: bytes, timeout: float = 10.0) -> str:
    with multiprocessing.Pool(1) as pool:
        result = pool.apply_async(_pdf_worker, (pdf_bytes,))
        try:
            return result.get(timeout=timeout)
        except multiprocessing.TimeoutError:
            pool.terminate()
            raise RuntimeError("PDF extraction timed out")
        except Exception as e:
            pool.terminate()
            raise RuntimeError(f"PDF extraction failed: {e!r}")
        


def get_calendar_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES_CALENDAR)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_file,'w') as f: f.write(creds.to_json())
        except RefreshError:
            os.remove(token_file)
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_file, SCOPES_CALENDAR
        )
        creds = flow.run_local_server(port=0, access_type='offline', prompt='consent')
        with open(token_file,'w') as f: f.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds)


def create_calendar_event(
    service,
    summary: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    timezone: str = 'UTC'
):
    event = {
        'summary': summary,
        'description': description,
        'start':   {'dateTime': start_dt.isoformat(), 'timeZone': timezone},
        'end':     {'dateTime': end_dt.isoformat(),   'timeZone': timezone},
    }
    return service.events().insert(calendarId='primary', body=event).execute()



def send_email_via_gmail(
    service,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    reply_to_msg_id: str | None = None,
):
    """
    Send a new message or a reply via Gmail API.
    - If thread_id is provided, the message is sent into that thread.
    - If reply_to_msg_id is provided, adds In-Reply-To & References headers.
    """
    # Build RFC 2822 email
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    if thread_id:
        msg["threadId"] = thread_id
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"] = reply_to_msg_id

    raw_bytes = base64.urlsafe_b64encode(msg.as_bytes())
    raw_str = raw_bytes.decode("utf-8")

    send_body: dict[str, Any] = {"raw": raw_str}
    if thread_id:
        send_body["threadId"] = thread_id

    return service.users().messages().send(
        userId="me",
        body=send_body
    ).execute()