import os
import base64
import fitz
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
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
from typing            import Tuple, Optional, Dict, Any
from datetime import datetime

from .config_private import ACCOUNTS

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

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

def _walk_parts(parts, service, msg_id, collected):
    """
    Recursively walk MIME parts, appending text to collected['plain']
    or collected['html'], and attachments to collected['pdfs'].
    """
    for part in parts:
        mime = part.get('mimeType','')
        filename = part.get('filename','') or ""
        body = part.get('body', {})
        data = body.get('data')
        if mime == 'text/plain' and data:
            text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            collected['plain'] += text + "\n"
        elif mime == 'text/html' and data:
            html = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            collected['html'] += html + "\n"
        # PDF attachment
        elif filename.lower().endswith('.pdf') and 'attachmentId' in body:
            request = lambda: service.users().messages().attachments().get(userId='me', messageId=msg_id, id=body["attachmentId"])
            att = safe_execute(request)
            pdf_data = base64.urlsafe_b64decode(att['data'])
            pdf_data = base64.urlsafe_b64decode(att['data'])
            collected['pdfs'].append(pdf_data)
        # If this part has children, recurse
        if 'parts' in part:
            _walk_parts(part['parts'], service, msg_id, collected)


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


def extract_text_from_pdf(data: bytes) -> str:
    doc = fitz.open(stream=data, filetype='pdf')
    return "\n".join(page.get_text() for page in doc)


def get_full_message_from_payload(
    service,
    raw: dict,
    load_attachments: bool = False
) -> Tuple[str, str, str, str, str, str, Optional[str], Optional[datetime]]:
    """
    Now requires:
      - service: the Gmail API client (to fetch attachments)
      - raw: the full message dict
    """
    # 1) Headers
    headers = {h['name']: h['value'] for h in raw['payload']['headers']}
    subject   = headers.get('Subject', '(no subject)')
    thread_id = raw.get('threadId', '')
    from_addr = headers.get('From', '')
    to_addr   = headers.get('To', '')

    # 2) Date parsing
    date_hdr = headers.get('Date')
    try:
        msg_dt   = parsedate_to_datetime(date_hdr)
        date_iso = msg_dt.date().isoformat()
    except Exception:
        msg_dt   = None
        date_iso = None

    # 3) Walk MIME parts to collect plain, html, and pdfs
    collected = {'plain': '', 'html': '', 'pdfs': []}

    def _walk(parts):
        for part in parts:
            mt = part.get('mimeType', '')
            fn = part.get('filename', '')
            body = part.get('body', {})
            data = body.get('data')
            if mt == 'text/plain' and data:
                txt = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                collected['plain'] += txt + "\n"
            elif mt == 'text/html' and data:
                html = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                collected['html'] += html + "\n"
            elif fn.lower().endswith('.pdf') and 'attachmentId' in body and load_attachments:
                # **Use the passed-in service** here:
                request = lambda: service.users().messages().attachments().get(userId='me', messageId=raw["id"], id=body["attachmentId"])
                att = safe_execute(request)
                pdf_data = base64.urlsafe_b64decode(att['data'])
                collected['pdfs'].append(pdf_data)
            # Recurse if nested parts
            if 'parts' in part:
                _walk(part['parts'])

    payload = raw['payload']
    if payload.get('parts'):
        _walk(payload['parts'])
    else:
        # single-part
        mt   = payload.get('mimeType','')
        data = payload.get('body',{}).get('data')
        if mt=='text/plain' and data:
            collected['plain'] = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
        elif mt=='text/html' and data:
            collected['html']  = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    # 4) Choose plain or fallback to stripped HTML
    if collected['plain'].strip():
        body = collected['plain']
    elif collected['html'].strip():
        soup = BeautifulSoup(collected['html'], 'html.parser')
        body = soup.get_text(separator='\n')
    else:
        body = ''

    # 5) Append PDF text
    for pdf in collected['pdfs']:
        try:
            doc = fitz.open(stream=pdf, filetype='pdf')
            text = "\n".join(page.get_text() for page in doc)
            body += "\n" + text
        except Exception:
            pass

    # 6) Snippet
    snippet = (body[:200] + '…') if len(body) > 200 else body
    snippet = snippet.replace('\n',' ')

    return subject, snippet, body, thread_id, from_addr, to_addr, date_iso, msg_dt