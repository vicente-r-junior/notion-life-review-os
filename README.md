# Notion Life Review OS

A WhatsApp-powered personal productivity assistant that captures your daily logs, tasks, projects, and learnings directly to Notion via conversational AI agents.

## Features

- **WhatsApp Integration** — send voice notes or text messages to capture your day naturally
- **Voice Transcription** — audio messages are automatically transcribed via OpenAI Whisper
- **Multi-Agent AI** — CrewAI agents handle extraction, matching, writing, and querying independently
- **Weekly Reports** — automated weekly productivity reviews delivered to your WhatsApp every Monday
- **Notion-Native Storage** — all data lives in your own Notion workspace, organized across databases
- **Message Aggregation** — multiple messages sent in quick succession are combined before processing
- **Conversational Confirmation** — review what was extracted before anything is saved
- **Query Interface** — ask questions about your data ("what tasks do I have today?")
- **Schema Management** — add new Notion database columns directly from WhatsApp
- **Observability** — structured JSON logging, health endpoint, and watchdog alerts
- **Redis Sessions** — conversation state and schema cache backed by Redis

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/vicente-r-junior/notion-life-review-os.git
cd notion-life-review-os
cp .env.example .env
# Edit .env with your API keys
```

### 2. Set up Notion databases

Create five Notion databases (Daily Logs, Tasks, Projects, Learnings, Weekly Reports) and copy their IDs into `.env`. Then run:

```bash
python scripts/setup_notion.py
```

### 3. Start the stack

```bash
docker compose up -d
```

### 4. Expose the webhook

Use a tunnel (e.g. ngrok or Cloudflare Tunnel) to expose port 8000, then configure your Evolution API instance to point to:

```
https://your-tunnel-url/webhook
```

with header `X-Webhook-Secret: <your WEBHOOK_SECRET>`.

### 5. Test locally

```bash
python scripts/test_message.py --text "worked on the app today, finished auth module"
python scripts/test_message.py --query "what tasks do I have this week?"
python scripts/test_message.py --command "*help*"
```

## Environment Variables

All configuration is done via `.env`. See `.env.example` for the full reference.

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key (GPT-4o + Whisper) |
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DB_*` | IDs of each Notion database |
| `MCP_URL` | URL of the Notion MCP server (default: `http://notion-mcp:3000`) |
| `MCP_AUTH_TOKEN` | Auth token for the MCP server |
| `EVOLUTION_API_URL` | Base URL of your Evolution API instance |
| `EVOLUTION_API_KEY` | Evolution API key |
| `EVOLUTION_INSTANCE` | Name of your WhatsApp instance |
| `WHATSAPP_NUMBER` | Your WhatsApp number (for weekly reports) |
| `WATCHDOG_PHONE` | Phone to receive service health alerts |
| `WEBHOOK_SECRET` | Secret header value to authenticate webhooks |
| `REDIS_URL` | Redis connection URL |
| `TIMEZONE` | Timezone for the weekly cron (e.g. `America/Sao_Paulo`) |
| `MESSAGE_AGGREGATION_SILENCE` | Seconds of silence before processing messages (default: 15) |
| `MESSAGE_AGGREGATION_WINDOW` | Max seconds before forcing processing (default: 45) |
| `SESSION_TTL` | Session expiry in seconds (default: 600) |
| `SCHEMA_CACHE_TTL` | Schema cache TTL in seconds (default: 3600) |
| `WEEKLY_REPORT_DAY` | Day of week for weekly report (default: `monday`) |
| `WEEKLY_REPORT_HOUR` | Hour of day for weekly report (default: `8`) |

## WhatsApp Commands

| Command | Description |
|---|---|
| `*help*` | Show all available commands |
| `*status*` | Check health of all services |
| `*week*` | Generate your weekly report now |
| `*undo*` | Delete the last entry (coming soon) |
| `*pause*` | Pause the bot for 24 hours |
| `*resume*` | Resume the bot |
| `*refresh*` | Refresh Notion schema cache |

## Architecture

The system is built on four main layers:

1. **Ingestion** — FastAPI webhook receives Evolution API events, performs idempotency checks, handles audio transcription, and buffers messages via Redis
2. **Routing** — the message router classifies intent (log / query / add_column) and dispatches accordingly
3. **Agents** — CrewAI agents powered by GPT-4o handle extraction, project matching, confirmation formatting, Notion writing, querying, and weekly analysis
4. **Storage** — Notion MCP server acts as the structured data layer; Redis stores session state and schema cache

### Key components

- **FastAPI** — HTTP server and webhook endpoint
- **CrewAI** — multi-agent orchestration framework
- **Notion MCP** — JSON-RPC server exposing Notion as structured tools
- **Redis** — session state, schema cache, idempotency keys, rate-limit flags
- **Evolution API** — WhatsApp Business API bridge
- **APScheduler** — weekly report cron job
- **structlog** — structured JSON logging with rotating file handlers

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full component diagram and data flow description.

## License

MIT — see [LICENSE](LICENSE).
