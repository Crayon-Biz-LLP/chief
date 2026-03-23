# Chief OS — Supabase Database Schema

> **Last updated:** 2026-03-22
> **Database:** Supabase (PostgreSQL)
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

| Column         | Type         | Notes                                          |
|----------------|--------------|------------------------------------------------|
| id             | bigint (PK)  | Auto-generated                                 |
| user_id        | text         | NOT NULL                                       |
| url            | text         | The link                                       |
| title          | text         | From og:title or AI extraction                 |
| summary        | text         | AI-generated strategic summary                 |
| category       | text         | `GITHUB` / `ARTICLE` / `X_THREAD` / `LINKEDIN` / `TOOL` / `LINK` |
| strategic_note | text         | AI note on why this matters                    |
| mission_id     | bigint (FK)  | References `missions.id`, nullable             |
| project_id     | bigint (FK)  | References `projects.id`, nullable             |
| created_at     | timestamptz  | Default `now()`                                |

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
```
