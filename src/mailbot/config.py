import os

MIN_IMPORTANCE_FOR_ALERT = 8

# Token & context budgets
INITIAL_MAX_INPUT_TOKENS  = 600     # for the quick classification
INITIAL_MAX_OUTPUT_TOKENS = 600
DEEP_MAX_INPUT_TOKENS     = 6000    # for full-body + attachments
DEEP_MAX_OUTPUT_TOKENS    = 8192

# Maximum time period to consider when scanning mailboxes
NUM_MESSAGES_LOOKBACK = 50

# Which initial‐importance triggers a “deep” pass?
DEEP_THRESHOLD_IMPORTANCE = 7
CALENDAR_IMPORTANCE_THRESHOLD = 7

POLL_INTERVAL_SECONDS = 120
PLANNING_INTERVAL_HOURS = 6

# llama-server HTTP endpoint
LLAMA_SERVER_URL   = "http://127.0.0.1:8080"
LLAMA_SERVER_MODEL = "Qwen3-14B-Q4_K_M"
LLAMA_CLI_PATH = os.environ["LLAMA_CLI_PATH"]

# Encrypted DB
DB_PATH     = "mailbot.db"

# Email labels
LABELS = ["Important", "Promotions", "Social", "Spam", "Receipts"]

# Keep your DB_PASSWORD in env
DB_PASSWORD = os.environ.get("MAILBOT_DB_PASSWORD", "CHANGE_ME")

# Agent settings
AGENT_ALWAYS_ASK_HUMAN = True

