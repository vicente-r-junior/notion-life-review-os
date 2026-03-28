# Notion Life Review OS

A WhatsApp assistant that saves your day to Notion. Send a voice note or a text about what you worked on, and it extracts tasks, projects, learnings, and mood — all organized in your own Notion workspace.

No apps to install. No dashboards to open. Just talk to it like a person.

---

## What it does

- Captures tasks, projects, learnings, and mood from natural messages
- Transcribes voice notes via Whisper
- Saves everything to your Notion databases
- Answers questions about your data ("what tasks are due this week?")
- Adds new columns to your Notion databases from WhatsApp
- Sends a weekly productivity review every Monday morning

---

## Stack

Python 3.12 · FastAPI · OpenAI GPT-4o + Whisper · Notion MCP · Evolution API · Redis · Docker

---

## Requirements

- A server or VPS with Docker (tested on Ubuntu 24)
- OpenAI API key
- Notion account with API integration
- WhatsApp number connected via Evolution API

---

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/vicente-r-junior/notion-life-review-os.git
cd notion-life-review-os
cp .env.example .env
```

### 2. Create your Notion databases

Create a parent page in Notion called **Life Review OS** and inside it create five databases:

- Daily Logs
- Tasks
- Projects
- Learnings
- Weekly Reports

Copy the database IDs from the URL of each database and paste them into `.env`.

Then connect your Notion integration to the parent page — it propagates to all child databases automatically.

### 3. Fill in your `.env`
```env
OPENAI_API_KEY=sk-...
WHISPER_LANGUAGE=          # leave empty for auto-detect; set to "en", "pt", etc. to force

NOTION_API_KEY=secret_...
NOTION_DB_DAILY_LOGS=...
NOTION_DB_TASKS=...
NOTION_DB_PROJECTS=...
NOTION_DB_LEARNINGS=...
NOTION_DB_WEEKLY_REPORTS=...

MCP_AUTH_TOKEN=any-random-string

EVOLUTION_API_URL=http://your-evolution-api:8080
EVOLUTION_API_KEY=...
EVOLUTION_INSTANCE=your-instance-name
WHATSAPP_NUMBER=5511999999999
WATCHDOG_PHONE=5511999999999  # receives service health alerts

REDIS_URL=redis://app-redis:6379
TIMEZONE=America/Sao_Paulo
```

### 4. Start the stack
```bash
docker compose up -d
```

This starts three containers: the app, the Notion MCP server, and Redis.

### 5. Configure the webhook

Point your Evolution API instance webhook to:
```
http://your-server:8000/webhook
```

If you're running locally, use a tunnel like [ngrok](https://ngrok.com) or [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

### 6. Test it
```bash
python scripts/test_message.py --text "worked on the API today, need to deploy by Friday"
python scripts/test_message.py --query "what tasks do I have this week?"
python scripts/test_message.py --command "*help*"
```

---

## WhatsApp commands

| Command | What it does |
|---|---|
| `*help*` | List all commands |
| `*status*` | Check if all services are running |
| `*week*` | Generate your weekly report now |
| `*pause*` | Mute the bot for 24 hours |
| `*resume*` | Unmute |
| `*refresh*` | Reload your Notion schema cache |

---

## Deploy (VPS)
```bash
git clone https://github.com/vicente-r-junior/notion-life-review-os.git
cd notion-life-review-os
cp .env.example .env
# edit .env
docker compose up -d
```

To update after changes:
```bash
git pull && docker compose restart app
```

---

## Project structure
```
app/
├── agents/          # Extractor, writer, query agent, weekly analyst
├── audio/           # Whisper transcription via Evolution API
├── notion/          # MCP client
├── router/          # Message routing and intent classification
├── scheduler/       # Weekly cron and aggregation worker
├── schema/          # Notion schema cache
├── session/         # Redis session and conversation history
└── whatsapp/        # Webhook handler and WhatsApp sender

prompts/             # LLM system prompts as markdown files
scripts/             # Local testing utilities
tests/               # Unit and integration test suite
```

---

## License

MIT
