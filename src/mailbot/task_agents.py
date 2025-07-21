# src/mailbot/task_agents.py

import re
import time
import requests
from datetime import datetime, timedelta
from typing import Any

from smolagents import CodeAgent, DuckDuckGoSearchTool, Tool, ToolCallingAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.models import OpenAIServerModel

from .config             import AGENT_ALWAYS_ASK_HUMAN
from .config     import LLAMA_SERVER_MODEL, LLAMA_SERVER_URL
from .config_private     import ACCOUNTS, USER_PROFILE_LLM_PROMPT_DEEP, USER_PERSONAL_IGNORE_CLAUSE, TIMEZONE
from .gmail_client       import get_service, fetch_full_message_payload, get_full_message_from_payload, create_calendar_event, get_calendar_service, send_email_via_gmail
from .db                 import get_conn, load_raw_message, get_message_history, get_contact_profile
from .telegram_message   import send_telegram, send_telegram_with_buttons
from .telegram_listener  import fetch_latest_user_reply

USER_CONFIRMATIONS: dict[tuple[str, str], bool] = {}

def _needs_permission_tag() -> str:
    return " NEEDS_USER_PERMISSION" if AGENT_ALWAYS_ASK_HUMAN else ""

class AskUserYesNoTool(Tool):
    name = "ask_user_yes_no"
    description = "Ask the user a yes/no question via Telegram inline buttons."
    inputs = {
        "tool": {
            "type": "string",
            "description": "Name of the tool to confirm (e.g. draft_reply)."
        },
        "details": {
            "type": "string",
            "description": "Additional context to display in the question "
                           "(if left equal to identifier, email details will be fetched)."
        }
    }
    output_type = "boolean"

    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id

    def forward(self, tool: str, details: str) -> bool:
        """
        Send a confirmation prompt; if `details` == `identifier`, auto-fetch email headers.
        Blocks until the user clicks Yes/No.
        """
        conn = get_conn()
        row = conn.execute(
            "SELECT from_addr, subject FROM emails WHERE msg_id = ?",
            (self.msg_id,)
        ).fetchone()
        if row:
            frm, subj = row
        else:
            frm, subj = "[unknown sender]", "[no subject]"

        prompt = (
            f"✉️ From: {frm}\n"
            f"📰 Subject: {subj}\n"
            f"{details}\n\n"
            f"Tool: {tool}\n"
            f"Msg ID: {self.msg_id}\n"
            "Proceed? ✅ Yes / ❌ No"
        )

        # Send inline buttons
        send_telegram_with_buttons(
            text=prompt,
            buttons=[
                {"text": "✅ Yes", "callback_data": "yes"},
                {"text": "❌ No",  "callback_data": "no"},
            ],
        )

        # Block until user clicks
        while True:
            choice = fetch_latest_user_reply()  # None until clicked
            if choice in ("yes", "no"):
                approved = (choice == "yes")
                if approved:
                    USER_CONFIRMATIONS[tool, self.msg_id] = True
                    return True
                else:
                    USER_CONFIRMATIONS[tool, self.msg_id] = False
                    return False
            time.sleep(1)


class GmailMarkSpamTool(Tool):
    name = "gmail_mark_spam"
    description = (
        "Mark a Gmail message as spam."
    )
    inputs = {
    }
    output_type = "string"


    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id


    def forward(self) -> str:
        acct_email = get_email_address(self.msg_id)
        acct = next((a for a in ACCOUNTS if a["email"] in acct_email), None)
        if not acct:
            return f"ERROR: No configured account for {acct_email}"


        svc = get_service(acct["credentials_file"], acct["token_file"])
        http = getattr(svc, "_http", None)

        try:
            svc.users().messages().modify(
                userId="me",
                id=self.msg_id,
                body={"addLabelIds": ["SPAM"]}
            ).execute()
            return f"Message {self.msg_id} marked as SPAM."
        except Exception as e:
            print("Exception occurred: ", e)
        finally:
            if http is not None and hasattr(http, "connections"):
                http.connections.clear()


class SendEmailTool(Tool):
    name = "send_email"
    description = "Send an email message."
    inputs = {
        "to":      {"type": "string", "description": "Recipient address"},
        "subject": {"type": "string", "description": "Email subject"},
        "body":    {"type": "string", "description": "Email body"},
        "is_reply": {"type": "boolean", "description": "If true, send as a reply in the same thread"},
    }
    output_type = "string"

    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id

    def forward(self, to: str, subject: str, body: str, is_reply: str) -> str:
        # choose same account as original
        conn = get_conn()
        row = conn.execute(
            "SELECT to_addr, thread_id FROM emails WHERE msg_id = ?",
             (self.msg_id,)
        ).fetchone()
        to_addr, thread_id = row
        acct = next(a for a in ACCOUNTS if a["email"] == to_addr)

        svc = get_service(acct["credentials_file"], acct["token_file"])

        send_email_via_gmail(
            service=svc,
            to=to,
            subject=subject,
            body=body,
            thread_id=(thread_id if is_reply else None),
            reply_to_msg_id=(self.msg_id if is_reply else None),
        )
        return f"Email sent to {to}."


# ——— Sub‑agents (managed) ————————————————————————————

def build_web_search_agent() -> ToolCallingAgent:
    """
    A small agent that takes a 'query' and uses DuckDuckGoSearchTool
    to return a summary via FinalAnswerTool.
    """
    tools = [
        DuckDuckGoSearchTool(),
        FinalAnswerTool(name="search_complete", description="Return search summary")
    ]
    model = OpenAIServerModel(model_id=LLAMA_SERVER_MODEL, api_base=LLAMA_SERVER_URL)
    return ToolCallingAgent(
        tools=tools,
        model=model,
        name="web_search_agent",
        description="Performs web searches and summarizes results."
    )


def build_draft_reply_agent(msg_id: str, thread_id: str) -> ToolCallingAgent:
    """
    Agent that drafts a reply to the email identified by msg_id within its thread.
    Includes:
      - Sender profile
      - Last N messages in the same thread (excluding this one)
      - Full body of this email
      - Tools for web search and clarifications
    """
    conn = get_conn()
    
    # 1) Full email body
    raw = load_raw_message(conn, mid)


    # 2) Sender profile
    profile = get_contact_profile(conn, frm) or {}

    # 3) Thread history (exclude current msg_id)
    history = get_message_history(conn, thread_id, limit=5, exclude_msg_id=msg_id)

    # --- Build system prompt ---
    system_prompt = (
        f"You are drafting a reply *on behalf of the user* to an email in thread {thread_id}.\n\n"
        f"✉️ Sender: {frm}\n"
        f"Profile: {profile}\n\n"
        f"📜 Thread history (most recent first):\n"
    )
    if history:
        for date, snippet in history:
            system_prompt += f"- {date}: {snippet}\n"
    else:
        system_prompt += "(no prior messages in this thread)\n"
    system_prompt += (
        "\nFull email body to reply to:\n"
        "----------\n"
        f"{body}\n"
        "----------\n\n"
        "Please draft a polite, concise reply that:\n"
        "1. Acknowledges the sender’s key points\n"
        "2. Answers any questions asked\n"
        "3. Follows the user’s style and respects tone\n\n"
        "If you need factual information, use DuckDuckGoSearchTool.\n"
        "If you need a yes/no clarification, use AskUserYesNoTool.\n"
        "If you need open‑ended clarifications, use TelegramUserTool.\n"
        "When your draft is ready, return it via the `draft_complete` FinalAnswerTool."
    )

    tools: list[Tool] = [
        DuckDuckGoSearchTool(),
        AskUserYesNoTool(msg_id),
        TelegramUserTool(),
        FinalAnswerTool(
            name="draft_complete",
            description="Return the drafted reply as a string"
        ),
    ]

    model = OpenAIServerModel(
        model_id=LLAMA_SERVER_MODEL,
        api_base=LLAMA_SERVER_URL,
    )

    return ToolCallingAgent(
        tools=tools,
        model=model,
        name="draft_reply_agent",
        description="Draft a context‑aware reply to an email thread.",
        system_prompt=system_prompt,
    )

# TODO: not currently using this tool because it involves a get request to a link found in the email body. 
#       Nees to be revised with security in mind.
class UnsubscribeTool(Tool): 
    name = "unsubscribe"
    description = (
        "Searches the full email body for unsub link and clicks it if it exists."
        + _needs_permission_tag()
    )
    inputs = {}
    output_type = "string"


    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id


    def forward(self) -> str:
        key = (self.name, self.msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {self.msg_id}"

        acct_email = get_email_address(self.msg_id)
        acct = next((a for a in ACCOUNTS if a["email"] == acct_email), None)
        if not acct:
            return f"ERROR: No configured account for {acct_email}"

        svc = get_service(acct["credentials_file"], acct["token_file"])

        raw = fetch_full_message_payload(svc, self.msg_id)
        html_body = get_full_message_from_payload(svc, raw)[2]
        match = re.search(r'href="([^"]+unsubscribe[^"]+)"', html_body, re.I)
        if match:
            url = match.group(1)
            requests.get(url, timeout=10)
            return f"Clicked unsubscribe link: {url}"
        
        return f"No unsubscribe link found in email;"

def get_email_address(msg_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT to_addr FROM emails WHERE msg_id = ?",
        (msg_id,)
    ).fetchone()
    return row[0]


class GmailCreateEventTool(Tool):
    name = "gmail_create_event"
    description = (
        "Create a Google Calendar event."
        + _needs_permission_tag()
    )
    inputs = {
        "title":       {"type":"string","description":"Event title."},
        "description": {"type":"string","description":"Event body."},
        "dt_str":      {"type":"string","description":"ISO datetime for start."}
    }
    output_type = "string"


    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id


    def forward(self, title: str, description: str, dt_str: str) -> str:
        key = (self.name, self.msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {self.msg_id}"

        start_dt = datetime.fromisoformat(dt_str)

        end_dt = start_dt + timedelta(minutes=30)

        event_body = {
            "summary":     title,
            "description": description or f"Event for email {self.msg_id}",
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": TIMEZONE,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": TIMEZONE,
            },
            "reminders": {
                "useDefault": True
            }
        }

        acct = ACCOUNTS[0]
        svc  = get_calendar_service(
            acct["calendar_credentials_file"],
            acct["calendar_token_file"]
        )

        created = svc.events().insert(calendarId="primary", body=event_body).execute()
        link = created.get("htmlLink", "")
        return f"Event created: {link}"
        
class ScheduleReminderTool(Tool):
    name = "schedule_reminder"
    description = (
        "Create a calendar event that acts as a reminder X hours before a future deadline."
        + _needs_permission_tag()
    )
    inputs = {
        "title":      {"type":"string","description":"Reminder title."},
        "deadline":   {"type":"string","description":"ISO datetime (deadline)."},
        "lead_hours": {"type":"integer","description":"How many hours before deadline to be reminded."},
    }
    output_type = "string"


    def __init__(self, msg_id: str):
        super().__init__()
        self.msg_id = msg_id

    def forward(self, title: str, deadline: str, lead_hours: int) -> str:
        key = (self.name, self.msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {self.msg_id}"
        # parse ISO timestamp
        start_dt = datetime.fromisoformat(deadline)
        end_dt   = start_dt + timedelta(minutes=30)

        event_body = {
            "summary":     title,
            "description": f"Reminder for email {self.msg_id}",
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": TIMEZONE,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": TIMEZONE,
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": lead_hours * 60},
                    {"method": "popup",  "minutes": lead_hours * 60},

                ]
            }
        }

        svc = get_calendar_service(
            ACCOUNTS[0]["calendar_credentials_file"],
            ACCOUNTS[0]["calendar_token_file"]
        )

        ev = svc.events().insert(calendarId="primary", body=event_body).execute()
        link = ev.get("htmlLink", "")
        return f"Reminder event created: {link}"


class TelegramUserTool(Tool):
    name = "ask_user"
    description = "Ask the user an open-ended question via Telegram."
    inputs = {
        "question": {"type":"string","description":"Question text."}
    }
    output_type = "string"

    def forward(self, question: str) -> str:
        send_telegram(question)
        # Stall until next user message arrives
        while True:
            reply = fetch_latest_user_reply()
            if reply:
                return reply
            time.sleep(1)


class TelegramReminderTool(Tool):
    name = "remind_user"
    description = "Send an immediate reminder or alert to the user through telegram."
    inputs = {
        "text": {"type":"string","description":"Brief reminder or alert summary."}
    }
    output_type = "string"

    def forward(self, text: str) -> str:
        message = f"⏰ *Reminder*\n\n{text}"
        send_telegram(message, html=False)


def handle_action(rec: dict):
    msg_id = rec['msg_id']

    tools = [
        AskUserYesNoTool(msg_id),
        GmailMarkSpamTool(msg_id),
        GmailCreateEventTool(msg_id),
        ScheduleReminderTool(msg_id),
        SendEmailTool(msg_id),
        TelegramUserTool(),
        TelegramReminderTool(),
        FinalAnswerTool(name="final_answer", description="Return the final answer to the user"),
    ]

    # Build sub‑agents
    managed_agents = [
        build_web_search_agent(),
        build_draft_reply_agent(msg_id),
    ]
    model = OpenAIServerModel(
        model_id=LLAMA_SERVER_MODEL,
        api_base=LLAMA_SERVER_URL,
    )
    agent = CodeAgent(
        tools=tools,
        managed_agents=managed_agents,
        model=model,
        max_steps=6,
        verbosity_level=2,
    )
    prompt = (
        f"You are an autonomous assistant for a user with profile:\n"
        f"{USER_PROFILE_LLM_PROMPT_DEEP}\n\n"
        "The user just received this email (already analyzed):\n"
        f"From: {rec['from']}, Subject: {rec['subject']}, Date: {rec['date']},\n"
        f"Snippet: {rec['snippet']}, Summary: {rec['summary']},\n"
        f"Category: {rec['category']}, Importance: {rec['importance']},\n"
        f"Msg Id: {rec['msg_id']}, Suggested action: {rec.get('action')}.\n\n"
        "Based on the email summary and intent, select and run\n"
        "the appropriate tool(s) to complete the action.\n"
        "You may search the web if you need GENERAL information.\n"
        "Some tools require you to ask the user for yes/no go-ahead first\n"
        "(indicated by NEEDS_USER_PERMISSION in their description)\n"
        "and will fail if you skip that step.\n"
        "If the user rejects your idea - you may suggest a different action or "
        "simply do nothing and consider your task complete.\n"
        "Pay close attention to the tool-responses as you do step-wise processing.\n"
        "For example, if you decide to ask the user a question in a former step, "
        "the answer will be provided in the tool-response's text Observation or Execution logs.\n\n"
        f"{USER_PERSONAL_IGNORE_CLAUSE}\n"
        "Finally - remember you may only answer in the form:\n"
        "Code:\n"  
        "```python\n"  
        "<your python code here>\n"  
        "```<end_code>\n"
    )

    final = agent.run(prompt)
    return final

