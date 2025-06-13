# src/mailbot/task_agents.py

import re
import time
import requests
from datetime import datetime, timedelta
import json
import logging
from typing import Any

from smolagents import CodeAgent, ToolCallingAgent, DuckDuckGoSearchTool, Tool
from smolagents import ActionStep, TaskStep, Timing
from smolagents.default_tools import UserInputTool, FinalAnswerTool, PythonInterpreterTool
from smolagents.models import OpenAIServerModel

from .config             import AGENT_ALWAYS_ASK_HUMAN
from .config     import LLAMA_SERVER_MODEL, LLAMA_SERVER_URL
from .config_private     import ACCOUNTS, USER_PROFILE_LLM_PROMPT_DEEP
from .calendar_client    import create_calendar_event, get_calendar_service
from .gmail_client       import get_service, fetch_full_message_payload, get_full_message_from_payload
from .db                 import add_task, mark_task_sent
from .telegram_message   import send_telegram, send_telegram_with_buttons
from .telegram_listener  import fetch_latest_user_reply
from .llm_client         import LlamaServerModel

# In‐memory record of which (tool, id) have been confirmed
USER_CONFIRMATIONS: dict[tuple[str, str], bool] = {}
TOOL_RESULTS: dict[tuple[str, str], Any] = {}

# ──────────────────────────────────────────────────────────────
# 1) The human‐ask tool (blocks until user replies)
# ──────────────────────────────────────────────────────────────
class AskUserYesNoTool(Tool):
    name = "ask_user_yes_no"
    description = "Ask the user a yes/no question via Telegram inline buttons."
    inputs = {
        "tool": {
            "type": "string",
            "description": "Name of the tool to confirm (e.g. gmail_mark_spam)."
        },
        "identifier": {
            "type": "string",
            "description": "The object id (e.g. message ID or event datetime)."
        },
        "details": {
            "type": "string",
            "description": "Additional context to display in the question "
                           "(if left equal to identifier, email details will be fetched)."
        }
    }
    output_type = "boolean"

    def forward(self, tool: str, identifier: str, details: str) -> bool:
        """
        Send a confirmation prompt; if `details` == `identifier`, auto-fetch email headers.
        Blocks until the user clicks Yes/No.
        """
        # If details is just the identifier, fetch email metadata
        if tool in ("gmail_mark_spam", "unsubscribe") and details == identifier:
            svc = get_service(
                ACCOUNTS[0]["credentials_file"],
                ACCOUNTS[0]["token_file"]
            )
            raw = fetch_full_message_payload(svc, identifier)
            subject, snippet, _, _, frm, _, _, _ = get_full_message_from_payload(svc, raw)
            details = f"✉️ From: {frm}\nSubject: {subject}\nSnippet: {snippet}"

        # Build the prompt
        prompt = (
            f"{details}\n\n"
            f"Tool: {tool}\n"
            f"Identifier: {identifier}\n"
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
                    USER_CONFIRMATIONS[tool, identifier] = True
                    return True
                else:
                    USER_CONFIRMATIONS[tool, identifier] = False
                    return False
            time.sleep(1)

# ──────────────────────────────────────────────────────────────
# 2) Irreversible tools that enforce the ask‐first policy
# ──────────────────────────────────────────────────────────────

def _needs_permission_tag() -> str:
    return " NEEDS_USER_PERMISSION" if AGENT_ALWAYS_ASK_HUMAN else ""

class GmailMarkSpamTool(Tool):
    name = "gmail_mark_spam"
    description = (
        "Mark a Gmail message as spam given its message ID."
        + _needs_permission_tag()
    )
    inputs = {
        "msg_id": {
            "type": "string",
            "description": "Gmail message ID to mark as spam."
        }
    }
    output_type = "string"

    def forward(self, msg_id: str) -> str:
        key = (self.name, msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {msg_id}"

        svc = get_service(
            ACCOUNTS[0]["credentials_file"],
            ACCOUNTS[0]["token_file"]
        )
        svc.users().messages().modify(
            userId="me", id=msg_id,
            body={"addLabelIds": ["SPAM"]}
        ).execute()
        return f"Message {msg_id} marked as SPAM."


class UnsubscribeTool(Tool):
    name = "unsubscribe"
    description = (
        "Searches the full email body for unsub link and clicks it if it exists."
        + _needs_permission_tag()
    )
    inputs = {
        "msg_id": {
            "type": "string",
            "description": "Gmail message ID to unsubscribe from."
        }
    }
    output_type = "string"

    def forward(self, msg_id: str) -> str:
        key = (self.name, msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {msg_id}"

        svc = get_service(
            ACCOUNTS[0]["credentials_file"],
            ACCOUNTS[0]["token_file"]
        )
        raw = fetch_full_message_payload(svc, msg_id)
        html_body = get_full_message_from_payload(svc, raw)[2]
        match = re.search(r'href="([^"]+unsubscribe[^"]+)"', html_body, re.I)
        if match:
            url = match.group(1)
            requests.get(url, timeout=10)
            return f"Clicked unsubscribe link: {url}"
        
        return f"No unsubscribe link found in email;"


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

    def forward(self, title: str, description: str, dt_str: str) -> str:
        key = (self.name, title + dt_str)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {title}@{dt_str}"

        svc = get_calendar_service(
            ACCOUNTS[0]["calendar_credentials_file"],
            ACCOUNTS[0]["calendar_token_file"]
        )
        start_dt = datetime.fromisoformat(dt_str)
        end_dt   = start_dt + timedelta(minutes=30)
        ev = create_calendar_event(
            svc, summary=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt
        )
        link = ev.get("htmlLink", "")
        return f"Event created: {link}"


class ScheduleReminderTool(Tool):
    name = "schedule_reminder"
    description = (
        "Schedule a reminder for the user."
        + _needs_permission_tag()
    )
    inputs = {
        "msg_id":     {"type":"string","description":"Gmail message ID."},
        "title":      {"type":"string","description":"Reminder title."},
        "dt_str":     {"type":"string","description":"ISO datetime."},
        "acct_email": {"type":"string","description":"Account email."}
    }
    output_type = "string"

    def forward(self, msg_id: str, title: str, dt_str: str, acct_email: str) -> str:
        key = (self.name, msg_id)
        if AGENT_ALWAYS_ASK_HUMAN and not USER_CONFIRMATIONS.get(key, False):
            return f"ERROR: Missing user confirmation for {self.name} on {msg_id}"

        sched_time = datetime.fromisoformat(dt_str) - timedelta(days=2)
        inserted = add_task(
            conn=None,  # agent context writes to DB
            msg_id=msg_id,
            type_='reminder',
            title=title,
            datetime=dt_str,
            acct_email=acct_email,
            scheduled_time=sched_time
        )
        if not inserted:
            return f"Reminder for {msg_id} already exists."
        return f"Reminder scheduled for {dt_str}."


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


def handle_action(rec: dict):
    tools = [
        AskUserYesNoTool(),
        GmailMarkSpamTool(),
        UnsubscribeTool(),
        GmailCreateEventTool(),
        ScheduleReminderTool(),
        DuckDuckGoSearchTool(),
        TelegramUserTool(),
        FinalAnswerTool(name="final_answer", description="Return the final answer to the user"),
    ]

    model = OpenAIServerModel(
        model_id=LLAMA_SERVER_MODEL,
        api_base=LLAMA_SERVER_URL,
    )
    agent = CodeAgent(
        tools=tools,
        model=model,
        max_steps=5,
        verbosity_level=1
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
        "the answer will be provided in the tool-response's text Observation or Execution logs.\n"
        "Finally - remember you may only answer in the form:\n"
        "Code:\n"  
        "```python\n"  
        "<your python code here>\n"  
        "```<end_code>\n"
    )

    final = agent.run(prompt)
    return final

