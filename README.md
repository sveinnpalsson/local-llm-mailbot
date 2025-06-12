# Local LLM Mailbot

**Local LLM Mailbot** is a private, local AI assistant for managing your email, calendar, and reminders. It uses a one-time profile build of your contacts based on your email history, then continuously listens to your inbox—classifying messages, scheduling events, sending notifications via Telegram and potentially completes more tasks (under development). All data is encrypted locally and processed with a LLM of your choice (e.g. I run Qwen3-14B_Q4_K_M on a RTX3090 card).

> ⚠️ **Disclaimer:** This is a **proof-of-concept** and **not** production-grade software. Use at your own risk. The project is under active development and contains several TODOs.

---

## 🚀 Key Features

- **One-Time Profile Builder**  
  Run `profile_builder.py` once to scan your Gmail INBOX/SENT threads. Generates a JSON “profile” for each contact (role, topics, tone, etc.) and saves it in the encrypted database.

- **Continuous Inbox Listener**  
  Run `main.py` to watch one or more of your Gmail inboxes via the History API. Each new message goes through:
  1. **Shallow pass**: quick categorization, importance scoring, summary  
  2. **Deep pass** (high-priority only): chain-of-thought reasoning, detailed summary, follow-up suggestions

- **AI-Driven Scheduling**  
  Automatically creates Google Calendar events or reminders for important emails. Idempotent scheduling tracked in the `tasks` table—no duplicate events on reruns.

- **Encrypted Data Storage**  
  Uses an encrypted SQLite database (`mailbot.db`) via `sqlcipher3`.  
  - Key taken from `MAILBOT_DB_PASSWORD` env var  
  - Main tables:  
    - `emails` (ID, date, sender, subject, summary, classification)  
    - `contacts` (profile JSON per address)  
    - `tasks` (scheduled events/reminders)  
    - `raw_messages` (cached full JSON payloads)  
    - `ignore_rules` (patterns/addresses to skip)

- **Local LLM Inference**  
  All AI processing uses your GPU via [llama-server](https://github.com/ggml-org/llama.cpp) and [llama-cli](https://github.com/ggml-org/llama.cpp). I recommend the [Qwen3-14B-GGUF model](https://huggingface.co/Qwen/Qwen3-14B-GGUF) (Apache-2.0).

- **Telegram Notifications**  
  Sends alerts through Telethon (or your Bot token) with Markdown/HTML links to emails or events. (End-to-end encryption is a TODO.)

---

## 📋 Prerequisites

- **OS:** Linux (WSL2/Ubuntu) with NVIDIA GPU & CUDA  
- **Python:** 3.11  
- **Google APIs:** Gmail & Calendar OAuth 2.0 credentials  
- **Telegram API:** Bot token or API ID/Hash  
- **LLM Model:** Quantized GGUF model (e.g. Qwen3-14B) in `model/`

---

## ⚙️ Setup Instructions

1. **Clone & install**  
   ```  
   git clone https://github.com/sveinnpalsson/local-llm-mailbot.git  
   cd local-llm-mailbot  
   python3.11 -m venv .venv  
   source .venv/bin/activate  
   pip install --upgrade pip setuptools  
   pip install -e .  
   pip install -r requirements.txt  
   ```

2. **Google OAuth**  
   - In Google Cloud Console, create OAuth 2.0 credentials for **Gmail API** and **Calendar API**.  
   - Download the JSON files and place them in `env/` (create it if missing):  
     - `env/credentials.json`  
     - `env/credentials_calendar.json`  
   - Copy `config_private_template.py` → `config_private.py` and setup your account details:
   - On first run (`profile_builder.py` or `main.py`), you’ll be prompted to authorize. Token files (e.g. `env/token_<account_name>.json`) are then auto-generated.

3. **Telegram Setup (optional)**  
   - Get a **Bot Token** from BotFather **or** your personal API ID/Hash from https://my.telegram.org.  
   - In `config_private.py`, set either `TELEGRAM_BOT_TOKEN` **or** both `TELEGRAM_API_ID` & `TELEGRAM_API_HASH`, plus `TELEGRAM_CHANNEL` (chat ID or “Saved Messages”).  

4. **Model Setup**  
   - Download your GGUF model (e.g. `Qwen3-14B-Q4_K_M.gguf`) into `models/`.  
   - Adjust the `llama-server` launch command:  
     ```  
     llama-server \
       -m models/Qwen3-14B-Q4_K_M.gguf \
       -c 32768 -n 8192 -ngl 99 --jinja \
       --presence-penalty 1.5 --host 127.0.0.1 --port 8080
     ```

---

## 🚦 Usage


1. **Start LLM Server**  
   (See Model Setup above.)

2. **Build Contact Profiles**  
   ```  
   python -m mailbot.profile_builder  
   ```

3. **Run the Listener**  
   ```  
    python -m mailbot.main  
   ```

Logs will show classification/scheduling steps and Telegram notifications when tasks are created.

---

## 🛠 Development Notes

- **Status:** Active development; many features marked TODO (e.g. end-to-end Telegram encryption, live contact updates).  
- **Extensibility:** The `classifier.py` framework lets you add new “tools” (RSS, Slack, etc.) for the agent to invoke.  
- **Debugging:** Raw email JSONs are cached in `raw_messages` for inspection.

---

## 📜 License

Released under the **MIT License** (see `LICENSE`). All dependencies are MIT or Apache-2.0-compatible.

---

