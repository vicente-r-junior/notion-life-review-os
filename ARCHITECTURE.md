# Architecture — Notion Life Review OS

## System Overview

Notion Life Review OS is a WhatsApp-to-Notion productivity pipeline. Users send free-form text or voice messages describing their day. The system transcribes audio, aggregates rapid-fire messages, classifies intent, extracts structured data via AI agents, presents a confirmation to the user, and finally writes to Notion databases through the Notion MCP server.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                          User (WhatsApp)                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ text / audio
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Evolution API                               │
│              (WhatsApp Business API bridge)                         │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP POST /webhook
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   FastAPI Application (port 8000)                   │
│                                                                     │
│  ┌─────────────┐   ┌──────────────────┐   ┌──────────────────────┐ │
│  │  /webhook   │──▶│  handler.py      │──▶│  audio/transcriber   │ │
│  │  /health    │   │  (idempotency,   │   │  (OpenAI Whisper)    │ │
│  │  /          │   │   routing,       │   └──────────────────────┘ │
│  └─────────────┘   │   commands)      │                            │
│                    └────────┬─────────┘                            │
│                             │                                      │
│                    ┌────────▼─────────┐                            │
│                    │  aggregation_    │   ┌────────────────────┐   │
│                    │  worker.py       │──▶│  message_router.py │   │
│                    │  (Redis buffer)  │   │  (intent classify) │   │
│                    └──────────────────┘   └────────┬───────────┘   │
│                                                    │               │
└────────────────────────────────────────────────────┼───────────────┘
                                                     │
                    ┌────────────────────────────────┘
                    │
         ┌──────────▼────────────────────────────────────┐
         │              CrewAI Agents Layer               │
         │                                               │
         │  ┌───────────┐  ┌──────────┐  ┌───────────┐  │
         │  │ extractor │  │ matcher  │  │confirma-  │  │
         │  │  (GPT-4o) │  │ (GPT-4o) │  │tion       │  │
         │  └─────┬─────┘  └────┬─────┘  └─────┬─────┘  │
         │        │             │               │        │
         │  ┌─────▼─────────────▼───────────────▼──────┐ │
         │  │         notion_writer (CrewAI Crew)       │ │
         │  └──────────────────┬────────────────────────┘ │
         │                     │                          │
         │  ┌──────────────────▼─────────────────────┐   │
         │  │  query_agent / weekly_analyst (Crews)   │   │
         │  └──────────────────┬─────────────────────┘   │
         └─────────────────────┼───────────────────────── ┘
                               │
                               ▼
         ┌─────────────────────────────────────────────┐
         │             Notion MCP Server               │
         │          (JSON-RPC over HTTP :3000)         │
         │                                             │
         │  tools: search, fetch, query-data-source,   │
         │          create-pages, update-page,         │
         │          retrieve-database, update-schema   │
         └─────────────────────┬───────────────────────┘
                               │
                               ▼
         ┌─────────────────────────────────────────────┐
         │              Notion Workspace               │
         │                                             │
         │  - Daily Logs database                      │
         │  - Tasks database                           │
         │  - Projects database                        │
         │  - Learnings database                       │
         │  - Weekly Reports database                  │
         └─────────────────────────────────────────────┘

         ┌─────────────────────────────────────────────┐
         │                  Redis                      │
         │                                             │
         │  - aggregating:{phone}  (message buffer)    │
         │  - session:{phone}      (conversation state)│
         │  - processed:{msg_id}   (idempotency)       │
         │  - schema:{db_name}     (schema cache)      │
         │  - paused:{phone}       (pause flag)        │
         │  - onboarded:{phone}    (onboarding flag)   │
         │  - rate_limit_notified  (watchdog cooldown) │
         └─────────────────────────────────────────────┘
```

## Data Flow

### Normal Message Flow

```
1. User sends WhatsApp message
2. Evolution API forwards to POST /webhook
3. handler.py checks idempotency (Redis processed:{msg_id})
4. If audio: transcribe via OpenAI Whisper API
5. Check for special commands (*help*, *status*, etc.)
6. Check for active session (confirmation waiting)
7. If onboarding: show welcome message
8. Add text to aggregation buffer (Redis aggregating:{phone})

--- aggregation_worker.py polls every 3 seconds ---

9. After SILENCE_SECONDS of no new messages (or WINDOW_SECONDS total):
   - Flush buffer, join messages
   - Send "Got everything! Processing now..."
   - Call message_router.process_log()

10. message_router classifies intent via GPT-4o + intent_classifier.md
11. If intent == "log":
    a. run_extractor() → structured JSON payload
    b. run_confirmation() → human-readable summary
    c. Save session with state="waiting_confirmation"
    d. Send confirmation to user

12. User replies "confirm" or "cancel"
13. If confirm: run_notion_writer() via CrewAI
    a. Upsert projects
    b. Create daily log entry
    c. Create tasks (linked to project + daily log)
    d. Create learnings (linked to daily log)
    e. Send "✅ Saved!" summary

14. If intent == "query":
    - run_query_agent() → query Notion → friendly answer

15. If intent == "add_column":
    - Start multi-step column creation flow via session states
```

### Weekly Report Flow

```
1. APScheduler fires on configured day/hour (default: Monday 08:00)
2. run_weekly_report() calls run_weekly_analyst()
3. weekly_analyst queries daily_logs, tasks, learnings for past 7 days
4. Generates mood trend, energy average, task stats, best learning
5. Creates a page in weekly_reports database
6. Sends WhatsApp-formatted report to WHATSAPP_NUMBER
```

## Agents and Their Responsibilities

### Extractor (`app/agents/extractor.py`)

A direct GPT-4o call (not a CrewAI crew) that receives the raw user text and database schemas, then returns a structured JSON payload containing:
- mood (1-5) and energy (low/medium/high)
- tags and one-line summary
- tasks with titles, projects, due dates, and custom fields
- project updates with progress notes
- learnings with insight text and area classification

Uses `prompts/extractor.md` as the system prompt.

### Matcher (`app/agents/matcher.py`)

A direct GPT-4o call that resolves ambiguous project name references against a list of existing Notion projects. Returns a match type (exact, fuzzy, ambiguous, none), the matched page ID, a confidence score, and candidate options for disambiguation.

Uses `prompts/matcher.md` as the system prompt.

### Confirmation (`app/agents/confirmation.py`)

A direct GPT-4o call that converts the structured payload into a human-readable WhatsApp message showing tasks, projects, learnings, mood, and energy. Ends with a "confirm / cancel" prompt.

Uses `prompts/confirmation.md` as the system prompt.

### Notion Writer (`app/agents/notion_writer.py`)

A CrewAI `Crew` with one agent and one task. The agent uses MCP tools to:
1. Upsert project pages
2. Create the daily log page
3. Create task pages (linked to project and daily log)
4. Create learning pages (linked to daily log)

Writes are done sequentially with 400ms delays to respect Notion rate limits. Failures are collected and reported without aborting the entire write.

Uses `prompts/notion_writer.md` as the task description.

### Query Agent (`app/agents/query_agent.py`)

A CrewAI `Crew` that answers free-form questions about the user's Notion data. It queries the appropriate database(s) using `query-data-source` with filters, then formats a conversational answer suitable for WhatsApp.

Uses `prompts/query_agent.md` as the task description.

### Weekly Analyst (`app/agents/weekly_analyst.py`)

A CrewAI `Crew` that generates the weekly productivity review. Queries the past 7 days of daily logs, tasks, and learnings, computes mood trends, energy averages, task stats, and writes a new weekly_reports page. Returns a WhatsApp-friendly formatted summary.

Uses `prompts/weekly_analyst.md` as the task description.

## Session State Management

Sessions are stored in Redis under `session:{phone}` with a TTL of `SESSION_TTL` seconds (default 600).

Each session has a `state` field that drives the conversation flow:

| State | Waiting For | Next State |
|---|---|---|
| `waiting_confirmation` | "confirm" or "cancel" | — (terminal) |
| `waiting_project_choice` | A number selecting a project | `waiting_confirmation` |
| `waiting_column_db` | A number selecting a database | `waiting_column_name` |
| `waiting_column_name` | The new column name | `waiting_column_type` |
| `waiting_column_type` | A number selecting a type | `waiting_column_options` or `waiting_column_required` |
| `waiting_column_options` | Comma-separated option values | `waiting_column_required` |
| `waiting_column_required` | "yes" or "no" | — (terminal, writes to Notion) |

## Schema Management

Database schemas are fetched from Notion MCP at startup and cached in Redis under `schema:{db_name}` for `SCHEMA_CACHE_TTL` seconds (default 3600).

Each cached schema contains:
- `database_id` — the Notion database UUID
- `data_source_id` — the MCP data source UUID (used for queries and writes)
- `fields` — map of field name to type, required flag, and Notion property ID

Agents receive schemas in their prompts so they can map field names to Notion property types without extra API calls.

The `*refresh*` command clears and re-fetches all schemas. The `diff_schemas()` function detects fields added directly in Notion since the last cache.

## Scheduler Jobs

| Job | Schedule | Function |
|---|---|---|
| Weekly Report | Configurable day + hour | `weekly_cron.run_weekly_report()` |
| Aggregation Worker | Every 3 seconds (async loop) | `aggregation_worker.aggregation_worker()` |
| Watchdog | Every 60 seconds (async loop) | `watchdog.watchdog_loop()` |

The weekly report and watchdog use `APScheduler` with `AsyncIOScheduler`. The aggregation worker runs as a plain `asyncio` task.

## Security

- Webhook requests are authenticated via the `X-Webhook-Secret` header
- Redis is not exposed externally (internal Docker network only)
- Notion MCP is not exposed externally (internal Docker network only)
- Phone numbers are masked in all log output (`mask_phone()`)
- Idempotency keys prevent duplicate processing of the same message ID
- Watchdog alerts use a 1-hour cooldown per service to prevent alert storms
