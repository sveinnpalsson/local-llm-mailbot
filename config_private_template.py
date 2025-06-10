"""
   This file shows a template for config_private.py with PLACEHOLDER values
   In copy this file to config_private.py, add your list of email accounts you want to monitor and process
   The first email account is assumed to be your 'main' account for which the google 
   calendar events and reminders are scheduled for.
"""
ACCOUNTS = [
    {
        "name": "main",
        "email": "example@gmail.com",
        "credentials_file": "env/credentials.json",
        "token_file":       "env/token_main.json",
        "calendar_credentials_file": "env/cal_credentials.json",
        "calendar_token_file":       "env/cal_token.json"
    },
    {
        "name": "secondary",
        "email": "example2@gmail.com",
        "credentials_file": "env/credentials.json",
        "token_file":       "env/token_secondary.json",
    }
]

# This gets injected into the system prompt to provide the LLM personal context
USER_PROFILE_LLM_PROMPT = ""
USER_PROFILE_LLM_PROMPT_DEEP = "" # For "deep" analysis
USER_PERSONAL_IGNORE_CLAUSE = ""

# Values needed if using telegram for notifications
TELEGRAM_BOT_TOKEN= ""
TELEGRAM_API_HASH = ""
TELEGRAM_API_ID = 1234567890
TELEGRAM_PHONE_NUMBER = '+1234567890'
TELEGRAM_CHANNEL  = 1234567890
