# calendar_client.py

import os
from datetime import datetime, timedelta
from google.oauth2.credentials      import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow      import InstalledAppFlow
from google.auth.exceptions         import RefreshError
from googleapiclient.discovery      import build

# Make sure your env/credentials.json & env/token_gc_*.json include calendar scopes
SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def get_calendar_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_file,'w') as f: f.write(creds.to_json())
        except RefreshError:
            os.remove(token_file)
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_file, SCOPES
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
