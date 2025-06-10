# Mail Butler

Mail Butler is your private, local AI assistant for managing email, calendar events, and reminders. The idea is to allow you to continously listen to your email inboxes, sort email based on your preferences and automatically schedule reminders, calendar events and send you telegram notifications on important events. The pipeline is highly customizable, allowing you to specify in prompt templates anything you want your personal AI assistant to consider when reading your email and making decisions about scheduling tasks. 

## 🚀 Key Features

### 1. Privacy-First, Local Processing  
- **On-device LLM inference** (e.g. Qwen3 via `llama-server`) runs entirely on your GPU—no third-party servers.  
- **Encrypted SQLite** stores only metadata (no raw email content unless you opt in).  
- **OAuth tokens** and credentials live in `env/` but you **must make sure to keep those safe** (e.g. never commit those to git).

### 2. Continuous Inbox Listener  
- Uses Gmail’s **History API** to detect new INBOX messages in real time (no full scans).  
- **Two-stage LLM pipeline** for each email:  
  1. **Shallow pass** → quick classification (category, importance, action, short summary)  
  2. **Deep pass** → chain-of-thought reasoning, detailed summary, contact-aware refinement for high-priority messages  

### 3. One-Time Profile Builder  
- Scans your INBOX & SENT threads to generate **JSON profiles** for every contact:  
  - **Role** (colleague, friend, vendor…)  
  - **Common Topics** (keywords they discuss)  
  - **Tone** (formal, casual)  
  - **Relationship** (your manager, project lead)  
  - **Notes** (other useful facts)  
- Profiles supercharge the deep-analysis stage, letting the LLM personalize its suggestions.

### 4. AI-Driven Calendar & Reminder Automation  
- **Iterative LLM agent** reviews each high-priority email and decides whether to:  
  - Schedule a **Calendar event** (date/time/duration)  
  - Create a **Date-only reminder**  
  - Skip if no action is needed  
- **Idempotent scheduling** via a `tasks` table—events and reminders never duplicate on reruns.  
- **Deep-link notifications**: Telegram alerts include tappable links to open the email thread in Gmail or the event in Google Calendar.

### 5. Telegram Notifications
- **Self-messages**: send through your own Telegram session (`Saved Messages`) or via a dedicated Bot API chat—your choice.  
- **HTML formatting**: bold text, native-app intent URIs, and web links for seamless mobile hand-off.  
- **Follow-up reminders**: scheduled ahead of deadlines (e.g. 2 days before) and delivered only once.

### 6. Extensible Agent Framework  
- Built on **function-calling** patterns: you can add new tools (RSS reader, Slack monitor, resume tailor, email composer).  
- The LLM itself decides *which* tool to invoke next, supporting multi-step workflows like “apply for job” with human-in-the-loop prompts.  
- **Memory & state**: encrypted persistence for tasks, contacts, and user preferences, enabling long-term assistance.

---

## 🛠️ Getting Started

### Prerequisites
- Python 3.11  
- WSL2/Ubuntu or Linux for GPU + CUDA support  
- NVIDIA RTX GPU (with 4-bit quantization)  
- Gmail & Google Calendar OAuth credentials  
- Telegram API credentials (Bot token or user-session `api_id`/`api_hash`)

### Installation
```
git clone https://github.com/sveinnpalsson/local-llm-mailbot.git
cd local-llm-mailbot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration
- Copy your OAuth JSONs into env/credentials_gmail.json and env/credentials_calendar.json
- Export environment variables:

```
export GOOGLE_GMAIL_CREDENTIALS="env/credentials_gmail.json"
export GOOGLE_CAL_CREDENTIALS="env/credentials_calendar.json"
export TG_API_ID=1234567
export TG_API_HASH="abcdef..."
export TELEGRAM_CHAT_ID="-1001234567890"
```

- Download & quantize your LLM into model/ (e.g. Qwen3-14B-Q4_K_M.gguf).

### Usage
- Profile Builder (one-time):
```
python profile_builder.py
```

- Start LLM server:
```
llama-server \
  -m models/qwen3-gguf/Qwen3-14B-Q4_K_M.gguf \
  -c 32768 -n 8192 -ngl 99 --jinja \ 
  --presence-penalty 1.5 --host 127.0.0.1 --port 8080
```

- Run Continuous Listener:
```
python continuous_main.py
```

### 🤝 Contributing
Contributions welcome!

### 📝 License
This project is licensed under the MIT License. See LICENSE for details.