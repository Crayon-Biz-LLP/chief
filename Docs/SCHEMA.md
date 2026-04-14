# Chief OS — Supabase Database Schema

> **Last updated:** 2025-07-07
> **Database:** Supabase (PostgreSQL + pgvector)
> **RLS:** Enabled on all tables — permissive policies via anon key

---

## Tables

### `core_config`
Key-value store for per-user state and settings.

| Column     | Type         | Notes                          |
|------------|--------------|--------------------------------|
| id         | bigint (PK)  | Auto-generated                 |
| user_id    | text         | `wa_<phone>` or Telegram chat ID |
| key        | text         | Config key name                |
| content    | text         | Config value                   |
| created_at | timestamptz  | Default `now()`                |

**Unique constraint:** `(user_id, key)`

**Known keys:**
| Key                   | Values / Format                              | Set By     |
|-----------------------|----------------------------------------------|------------|
| `invite_status`       | `approved`                                   | whatsapp   |
| `user_name`           | First name from WhatsApp profile              | whatsapp   |
| `joined_at`           | ISO 8601 timestamp                           | whatsapp   |
| `timezone_offset`     | GMT offset string (e.g. `5.5`, `-5`, `0`)    | whatsapp   |
| `mission_mode`        | `fix` / `grow` / `build` / `rest`            | whatsapp   |
| `pulse_schedule`      | `1` (early) / `2` (standard) / `3` (late)   | whatsapp   |
| `current_season`      | Free text — user's 14-day goal               | whatsapp   |
| `initial_people_setup`| `true`                                       | whatsapp   |
| `last_pulse_at`       | ISO 8601 timestamp (race condition lock)     | pulse      |
| `_pending_change`     | `schedule` / `mode` / `goal` / `timezone`    | whatsapp   |
| `google_connected`    | `true`                                       | auth       |
| `identity`            | `1` (Commander) / `2` (Architect) / `3` (Nurturer) | whatsapp |

---

### `raw_dumps`
Unstructured user thoughts — the input buffer for Pulse AI.

| Column       | Type         | Notes                        |
|--------------|--------------|------------------------------|
| id           | bigint (PK)  | Auto-generated               |
| user_id      | text         | NOT NULL                     |
| content      | text         | Raw message text             |
| source       | text         | `whatsapp` / `telegram`      |
| is_processed | boolean      | Default `false`, set `true` after Pulse |
| created_at   | timestamptz  | Default `now()`              |

---

### `tasks`
Structured action items extracted by Pulse AI.

| Column              | Type         | Notes                                    |
|---------------------|--------------|------------------------------------------|
| id                  | bigint (PK)  | Auto-generated                           |
| user_id             | text         | NOT NULL                                 |
| title               | text         | Task description                         |
| priority            | text         | `urgent` / `important` / `chores` / `ideas` |
| status              | text         | `todo` / `done` / `cancelled`            |
| project_id          | bigint (FK)  | References `projects.id`                 |
| estimated_minutes   | integer      | AI-estimated effort                      |
| duration_mins       | integer      | Actual/refined duration for scheduling   |
| is_revenue_critical | boolean      | Default `false`                          |
| deadline            | timestamptz  | Optional hard deadline                   |
| reminder_at         | timestamptz  | Snooze/defer — hide until this time      |
| completed_at        | timestamptz  | Set when status → `done`                 |
| google_task_id      | text         | Google Tasks item ID (if synced)         |
| google_event_id     | text         | Google Calendar event ID (if synced)     |
| created_at          | timestamptz  | Default `now()`                          |

---

### `projects`
User's project categories for task routing.

| Column     | Type         | Notes                                       |
|------------|--------------|---------------------------------------------|
| id         | bigint (PK)  | Auto-generated                              |
| user_id    | text         | NOT NULL                                    |
| name       | text         | Project name                                |
| org_tag    | text         | `BIZ` / `CHURCH` / `PERSONAL` / `INBOX`    |
| status     | text         | `active` / `archived`                       |
| context    | text         | `work` / `personal`                         |
| created_at | timestamptz  | Default `now()`                             |

---

### `people`
Stakeholders auto-detected from user dumps by Pulse AI.

| Column           | Type         | Notes                         |
|------------------|--------------|-------------------------------|
| id               | bigint (PK)  | Auto-generated               |
| user_id          | text         | NOT NULL                     |
| name             | text         | Person's name                |
| role             | text         | Nullable — relationship/role |
| strategic_weight | integer      | 1–10, default 5             |
| created_at       | timestamptz  | Default `now()`              |

---

### `logs`
AI-generated insights, sparks, and observations.

| Column     | Type         | Notes                            |
|------------|--------------|----------------------------------|
| id         | bigint (PK)  | Auto-generated                   |
| user_id    | text         | NOT NULL                         |
| entry_type | text         | `LOG` / `IDEAS` / `SPARK`        |
| content    | text         | AI-generated text                |
| created_at | timestamptz  | Default `now()`                  |

---

### `resources`
URLs and links captured from user dumps, enriched by Pulse AI.

| Column         | Type           | Notes                                          |
|----------------|----------------|------------------------------------------------|
| id             | bigint (PK)    | Auto-generated                                 |
| user_id        | text           | NOT NULL                                       |
| url            | text           | The link                                       |
| title          | text           | From og:title or AI extraction                 |
| summary        | text           | AI-generated strategic summary                 |
| category       | text           | `GITHUB` / `ARTICLE` / `X_THREAD` / `LINKEDIN` / `TOOL` / `LINK` |
| strategic_note | text           | AI note on why this matters                    |
| embedding      | vector(768)    | Gemini embedding for semantic search           |
| enriched_at    | timestamptz    | When AI last enriched this resource            |
| mission_id     | bigint (FK)    | References `missions.id`, nullable             |
| project_id     | bigint (FK)    | References `projects.id`, nullable             |
| created_at     | timestamptz    | Default `now()`                                |

---

### `missions`
Strategic goals (future use — auto-detected from patterns).

| Column      | Type         | Notes                              |
|-------------|--------------|------------------------------------|
| id          | bigint (PK)  | Auto-generated                     |
| user_id     | text         | NOT NULL                           |
| title       | text         | NOT NULL                           |
| description | text         | Optional detail                    |
| status      | text         | `active` / `completed` / `archived` |
| created_at  | timestamptz  | Default `now()`                    |

---

### `user_google_tokens`
Per-user OAuth 2.0 tokens for Google Calendar & Tasks integration.

| Column        | Type         | Notes                                   |
|---------------|--------------|-----------------------------------------|
| user_id       | text (PK)    | `wa_<phone>` — one token set per user   |
| access_token  | text         | NOT NULL — short-lived (~1 hour)        |
| refresh_token | text         | NOT NULL — long-lived, used to refresh  |
| token_expiry  | timestamptz  | When access_token expires               |
| scopes        | text         | Granted OAuth scopes                    |
| created_at    | timestamptz  | Default `now()`                         |
| updated_at    | timestamptz  | Default `now()`, auto-updated on refresh|

---

## Billing & Admin Tables

### `subscriptions`
Per-user subscription/plan management. One row per user.

| Column      | Type         | Notes                                                |
|-------------|--------------|------------------------------------------------------|
| id          | uuid (PK)    | `gen_random_uuid()`                                  |
| user_id     | text         | NOT NULL, UNIQUE — `wa_<phone>` or Telegram chat ID  |
| plan        | text         | `trial` / `pro` / `unlimited` — default `trial`      |
| status      | text         | `active` / `expired` / `suspended` — default `active` |
| trial_days  | integer      | Default `14` — configurable per user                  |
| started_at  | timestamptz  | Default `now()` — when plan began                     |
| expires_at  | timestamptz  | NULL = never expires (unlimited plan)                 |
| extended_by | text         | Admin user_id who last extended                       |
| notes       | text         | Admin annotations                                    |
| created_at  | timestamptz  | Default `now()`                                      |
| updated_at  | timestamptz  | Default `now()`                                      |

**Trigger:** `set_trial_expiry` — auto-sets `expires_at` on insert for trial plans.

---

### `usage_events`
Append-only log of every billable/trackable action. Used for analytics.

| Column     | Type         | Notes                                                  |
|------------|--------------|--------------------------------------------------------|
| id         | uuid (PK)    | `gen_random_uuid()`                                    |
| user_id    | text         | NOT NULL                                               |
| event_type | text         | `message_in` / `message_out` / `pulse` / `brain_query` / `research` / `media_process` |
| channel    | text         | `whatsapp` / `telegram` / `system`                     |
| metadata   | jsonb        | Default `{}` — extra context                           |
| created_at | timestamptz  | Default `now()`                                        |

**Indexes:** `(user_id, created_at DESC)`, `(event_type, created_at DESC)`

---

## New Tables (Memory & Graph Layer)

### `memories`
Long-term memory vault — notes, experiences, and extracted insights with vector embeddings.

| Column       | Type           | Notes                                        |
|--------------|----------------|----------------------------------------------|
| id           | uuid (PK)      | `gen_random_uuid()`                          |
| user_id      | text           | NOT NULL                                     |
| content      | text           | NOT NULL — the memory text                   |
| memory_type  | text           | `note` / `experience` / `insight` / `aar`    |
| embedding    | vector(768)    | Gemini embedding for semantic search          |
| metadata     | jsonb          | Default `{}` — arbitrary structured metadata  |
| created_at   | timestamptz    | Default `now()`                              |

**Index:** IVFFlat on `embedding` with `vector_cosine_ops`, 50 lists

**RPC:** `match_memories(query_embedding, match_threshold, match_count, filter_user_id)` — cosine similarity search

---

### `graph_nodes`
Knowledge graph vertices — entities extracted from user input.

| Column     | Type         | Notes                                            |
|------------|--------------|--------------------------------------------------|
| id         | uuid (PK)    | `gen_random_uuid()`                              |
| user_id    | text         | NOT NULL                                         |
| label      | text         | NOT NULL — entity name (e.g. "Acme Corp")        |
| type       | text         | `person` / `project` / `concept` / `place` / etc |
| metadata   | jsonb        | Default `{}` — extra properties                   |
| created_at | timestamptz  | Default `now()`                                  |

**Unique constraint:** `(user_id, label)` — one node per label per user

---

### `graph_edges`
Knowledge graph relationships between nodes.

| Column     | Type         | Notes                                         |
|------------|--------------|-----------------------------------------------|
| id         | uuid (PK)    | `gen_random_uuid()`                           |
| user_id    | text         | NOT NULL                                      |
| source_id  | uuid (FK)    | References `graph_nodes.id` ON DELETE CASCADE  |
| target_id  | uuid (FK)    | References `graph_nodes.id` ON DELETE CASCADE  |
| relation   | text         | NOT NULL — e.g. `works_at`, `depends_on`       |
| metadata   | jsonb        | Default `{}`                                  |
| created_at | timestamptz  | Default `now()`                               |

---

### `agent_queue`
Queue for autonomous agent tasks (research, delegation).

| Column      | Type         | Notes                                       |
|-------------|--------------|---------------------------------------------|
| id          | uuid (PK)    | `gen_random_uuid()`                         |
| user_id     | text         | NOT NULL                                    |
| task        | text         | NOT NULL — what the agent should do          |
| status      | text         | `pending` / `running` / `done` / `failed`   |
| result      | text         | Agent output, nullable                       |
| created_at  | timestamptz  | Default `now()`                             |
| finished_at | timestamptz  | When agent completed                         |

---

## Multi-Tenancy Pattern

All tables use `user_id` (text) for tenant isolation:
- **WhatsApp users:** `wa_<phone_number>` (e.g. `wa_919876543210`)
- **Telegram users:** Telegram `chat_id` as string (e.g. `123456789`)

RLS is enabled on all tables with permissive `USING (true) WITH CHECK (true)` policies — access control is enforced at the application layer via `user_id` filtering on every query.

---

## Foreign Keys

```
tasks.project_id        → projects.id
resources.project_id    → projects.id
resources.mission_id    → missions.id  (ON DELETE SET NULL)
graph_edges.source_id   → graph_nodes.id  (ON DELETE CASCADE)
graph_edges.target_id   → graph_nodes.id  (ON DELETE CASCADE)
```

---

## RPC Functions

### `match_memories(query_embedding, match_threshold, match_count, filter_user_id)`
Cosine similarity search over `memories.embedding`. Returns `id, user_id, content, memory_type, metadata, similarity`.

### `match_resources(query_embedding, match_threshold, match_count, filter_user_id)`
Cosine similarity search over `resources.embedding`. Returns `id, user_id, url, title, summary, category, strategic_note, similarity`.

---

## Extensions

- **pgvector** — `CREATE EXTENSION IF NOT EXISTS vector;`
