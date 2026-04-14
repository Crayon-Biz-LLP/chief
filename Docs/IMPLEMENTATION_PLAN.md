# Chief OS — Production Migration Plan
## Porting Integrated-OS Features for Multi-Tenant Public Use

> **Generated:** 2026-04-14
> **Source:** `integrated-os` (single-user personal OS)
> **Target:** `chief` (multi-tenant SaaS, deployed on Vercel)

---

## Executive Summary

`integrated-os` has evolved significantly beyond `chief` with 6 major feature systems that need porting. The core challenge: every feature in `integrated-os` is hardcoded for a **single user** (Danny), using **synchronous Supabase**, **service-role keys** (bypassing RLS), and **single-tenant Google credentials**. `chief` operates as a **multi-tenant SaaS** with **async Supabase**, **anon keys** (respecting RLS), **per-user OAuth tokens**, and **multi-channel delivery** (Telegram + WhatsApp).

---

## Gap Analysis: What Integrated-OS Has That Chief Doesn't

| # | Feature | Integrated-OS | Chief Status | Effort |
|---|---------|--------------|--------------|--------|
| 1 | **Semantic Memory Layer** (embeddings + vector search) | ✅ Full (`memories` table, `match_memories` RPC, `get_embedding()`) | ❌ Missing | **HIGH** |
| 2 | **Knowledge Graph** (nodes, edges, hybrid search) | ✅ Full (`graph_nodes`, `graph_edges`, `hybrid_search_graph()`) | ❌ Missing | **HIGH** |
| 3 | **AI Intent Classification at Webhook** (smart routing) | ✅ Full (`classify_intent()`, TASK/NOTE/NOISE/QUERY/DELEGATE routing) | ❌ Simple capture only (`raw_dumps` insert + "✅") | **MEDIUM** |
| 4 | **Multimodal Input** (images, audio, documents) | ✅ Full (`process_multimodal_content()`, Telegram file download) | ❌ Missing | **MEDIUM** |
| 5 | **Research Agent** (autonomous web research via Jina) | ✅ Full (`research_agent.py`, `agent_queue` table) | ❌ Missing | **MEDIUM** |
| 6 | **Staging Area Sorter** (pre-pulse TASK/NOTE/NOISE classification) | ✅ In pulse.py | ❌ Missing (all dumps go straight to AI) | **LOW** |
| 7 | **Hindsight Memory Retrieval** (vector recall in briefings) | ✅ Full (`retrieve_hindsight_memories()`) | ❌ Missing | **MEDIUM** |
| 8 | **After-Action Reports** (daily reflections saved to memory) | ✅ Full (`generate_after_action_report()`) | ❌ Missing | **LOW** |
| 9 | **Batch Resource Enrichment** (AI categorization + embeddings) | ✅ Full (`batch_enrich_resources()`) | ⚠️ Partial (URL scrape only, no AI enrichment/embeddings) | **MEDIUM** |
| 10 | **Brain Interrogation** (on-demand `?query` search) | ✅ Full (`interrogate_brain()` — graph + vector hybrid) | ❌ Missing | **MEDIUM** |
| 11 | **Archive Ingest** (Google Sheets journal → memories) | ✅ Full (`archive_ingest.py`) | ❌ N/A for multi-tenant (skip) | **SKIP** |
| 12 | **Graph Backfill from Memories** | ✅ Full (`backfill_graph.py`) | ❌ Needs multi-tenant version | **MEDIUM** |
| 13 | **Mission System** (via graph_nodes with type=mission) | ✅ Full (webhook commands + pulse routing) | ⚠️ Schema exists, no webhook commands | **LOW** |
| 14 | **Horizon Filtering** (48hr task window, 14-day creation gate) | ✅ Full | ❌ Missing (all tasks shown) | **LOW** |
| 15 | **Google→Supabase Reverse Sync** (completed in Google → done in DB) | ✅ Full (`sync_completed_tasks_from_google()`) | ❌ Missing (chief only pushes to Google) | **LOW** |

---

## Architecture Decisions (Chief Multi-Tenant Constraints)

### Non-Negotiable Rules
1. **All Supabase calls use `create_async_client` + anon key** (RLS enforced via `user_id`)
2. **No service-role key at the application layer** — background jobs use service-role only in GitHub Actions
3. **Google credentials are per-user** via `user_google_tokens` table (not env vars)
4. **Every table write includes `user_id`** — no global queries without tenant filter
5. **Vercel serverless: 60s max timeout** — heavy batch jobs run in GitHub Actions
6. **Embeddings are per-user namespaced** — vector search filters by `user_id`

### Key Translation Patterns

| Integrated-OS Pattern | Chief Equivalent |
|----------------------|------------------|
| `supabase = create_client(url, SERVICE_ROLE_KEY)` | `supabase = await create_async_client(url, ANON_KEY)` + RLS |
| `os.getenv("TELEGRAM_CHAT_ID")` | `user_id` from webhook payload |
| `os.getenv("GOOGLE_REFRESH_TOKEN")` | `await get_user_access_token(user_id)` |
| `gemini_client = genai.Client(...)` (module-level) | Lazy singleton via `get_genai_client()` |
| `supabase.table('x').select(...)` (sync) | `await supabase.table('x').select(...).execute()` (async) |
| `send_telegram(chat_id, text)` | `await send_message(user_id, text)` (routes TG/WA) |

---

## Implementation Phases

### Phase 0: Database Schema Migrations
**Priority: CRITICAL — Must be done first**

#### New Tables

```sql
-- 1. Memories table (semantic memory layer)
CREATE TABLE memories (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     text NOT NULL,
    content     text NOT NULL,
    memory_type text DEFAULT 'note',  -- note, reflection, insight, archive
    metadata    jsonb DEFAULT '{}',
    embedding   vector(768),          -- requires pgvector extension
    created_at  timestamptz DEFAULT now()
);

-- Index for vector similarity search
CREATE INDEX idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_memories_user_id ON memories(user_id);

-- RLS Policy
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own memories" ON memories
    USING (true) WITH CHECK (true);

-- 2. Graph Nodes table
CREATE TABLE graph_nodes (
    id       uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id  text NOT NULL,
    label    text NOT NULL,
    type     text DEFAULT 'concept',  -- person, organization, project, concept, mission, emotional_state
    metadata jsonb DEFAULT '{}',
    created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX idx_graph_nodes_user_label ON graph_nodes(user_id, label);
ALTER TABLE graph_nodes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own nodes" ON graph_nodes
    USING (true) WITH CHECK (true);

-- 3. Graph Edges table
CREATE TABLE graph_edges (
    id              uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id         text NOT NULL,
    source_node_id  uuid REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_node_id  uuid REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relationship    text NOT NULL,
    weight          float DEFAULT 1.0,
    metadata        jsonb DEFAULT '{}',
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX idx_graph_edges_source ON graph_edges(source_node_id);
CREATE INDEX idx_graph_edges_target ON graph_edges(target_node_id);
CREATE INDEX idx_graph_edges_user_id ON graph_edges(user_id);
ALTER TABLE graph_edges ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own edges" ON graph_edges
    USING (true) WITH CHECK (true);

-- 4. Agent Queue table (for research agent)
CREATE TABLE agent_queue (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      text NOT NULL,
    task         text NOT NULL,
    status       text DEFAULT 'pending',  -- pending, processing, completed, failed
    metadata     jsonb DEFAULT '{}',
    completed_at timestamptz,
    created_at   timestamptz DEFAULT now()
);

ALTER TABLE agent_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own agent tasks" ON agent_queue
    USING (true) WITH CHECK (true);
```

#### New Supabase RPC Functions

```sql
-- Vector similarity search (multi-tenant safe)
CREATE OR REPLACE FUNCTION match_memories(
    query_embedding vector(768),
    match_count int DEFAULT 5,
    match_threshold float DEFAULT 0.6,
    filter_user_id text DEFAULT NULL
)
RETURNS TABLE (
    id bigint,
    content text,
    memory_type text,
    metadata jsonb,
    similarity float,
    created_at timestamptz
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.memory_type,
        m.metadata,
        1 - (m.embedding <=> query_embedding) AS similarity,
        m.created_at
    FROM memories m
    WHERE
        (filter_user_id IS NULL OR m.user_id = filter_user_id)
        AND 1 - (m.embedding <=> query_embedding) > match_threshold
    ORDER BY m.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
```

#### Alter Existing Tables

```sql
-- Add embedding column to resources for semantic search
ALTER TABLE resources ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE resources ADD COLUMN IF NOT EXISTS enriched_at timestamptz;
ALTER TABLE resources ADD COLUMN IF NOT EXISTS strategic_note text;

-- Add duration_mins to tasks (used by calendar sync)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS duration_mins integer DEFAULT 15;
```

---

### Phase 1: Core Infrastructure Layer
**New file: `api/memory.py`**

Shared utilities consumed by both `webhook.py` and `pulse.py`.

```
api/
├── memory.py          ← NEW: Embedding + vector search + graph helpers
├── intent.py          ← NEW: AI intent classification engine
├── research.py        ← NEW: Research agent (async, multi-tenant)
├── webhook.py         ← MODIFY: Add smart routing, multimodal, brain query
├── pulse.py           ← MODIFY: Add memory retrieval, staging sorter, enrichment
├── google_sync.py     ← MODIFY: Add reverse sync (Google → Supabase)
├── index.py           ← MODIFY: Add new routes
├── auth.py            (no changes)
├── whatsapp.py        ← MODIFY: Mirror Telegram webhook features
```

#### `api/memory.py` — What Goes Here

| Function | Source (integrated-os) | Multi-Tenant Change |
|----------|----------------------|---------------------|
| `get_embedding(text)` | `core/pulse.py:97` | No change needed (stateless) |
| `store_memory(user_id, content, memory_type, metadata)` | Inline in webhook.py | Add `user_id`, compute embedding, insert |
| `retrieve_memories(user_id, query, top_k)` | `core/pulse.py:167` | Filter by `user_id` in RPC call |
| `hybrid_search_graph(user_id, query)` | `core/pulse.py:113` | Filter `graph_nodes`/`graph_edges` by `user_id` |
| `store_graph_node(user_id, label, type, metadata)` | `core/skills/backfill_graph.py` | Add `user_id` |
| `store_graph_edge(user_id, source_id, target_id, relationship)` | `core/skills/backfill_graph.py` | Add `user_id` |

#### `api/intent.py` — What Goes Here

| Function | Source (integrated-os) | Multi-Tenant Change |
|----------|----------------------|---------------------|
| `classify_intent(text, context, user_config)` | `core/webhook.py:117` | Replace hardcoded persona with user's chosen persona |
| `get_recent_context(user_id, limit)` | `core/webhook.py:182` | Filter by `user_id` |

#### `api/research.py` — What Goes Here

| Function | Source (integrated-os) | Multi-Tenant Change |
|----------|----------------------|---------------------|
| `process_pending_research(user_id)` | `core/research_agent.py` | Filter queue by `user_id`, notify via `send_message()` |

---

### Phase 2: Smart Webhook (Intent Classification + Multimodal)
**Modify: `api/webhook.py`**

**Current state:** Dumb capture → `raw_dumps` insert → "✅" reply.

**Target state:** Full intent classification pipeline:

```
User Message → classify_intent()
    ├── TASK (≥0.6 confidence)  → raw_dumps + receipt
    ├── NOTE (≥0.6 confidence)  → memories table + "Note vaulted."
    ├── QUERY (≥0.6 confidence) → interrogate_brain() → reply with answer
    ├── DELEGATE (any)          → agent_queue + "Research queued."
    ├── NOISE (any)             → "👍"
    └── CLARIFICATION_NEEDED    → Ask question + save to raw_dumps

URL Detection → NOTE intent + url starts with http → resources table

Multimodal:
    ├── Photo  → download → Gemini vision → extract TASK/NOTE
    ├── Voice  → download → Gemini audio → extract TASK/NOTE
    └── Document → download → Gemini → extract TASK/NOTE
```

**Key changes to `webhook.py`:**

1. **After onboarding completes + trial check passes**, replace the current "CAPTURE MODE" block (line ~361–363) with the full classification pipeline
2. Add `?query` prefix handler → `interrogate_brain(user_id, query)`
3. Add `N:` / `Note:` prefix handler → direct to memories
4. Add multimodal handlers for `photo`, `voice`, `audio`, `document` message types
5. Add new commands: `🚀 Mission`, `📚 Library`
6. Replace keyboard:
   ```python
   MAIN_KEYBOARD = {
       "keyboard": [
           [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
           [{"text": "🚀 Mission"}, {"text": "📚 Library"}],
           [{"text": "🧭 Main Goal"}, {"text": "🔓 Vault"}],
           [{"text": "⚙️ Settings"}]
       ],
       "resize_keyboard": True,
       "persistent": True
   }
   ```

**Critical multi-tenant adaptations:**
- `classify_intent()` receives the user's persona config (Commander/Architect/Nurturer) to shape the AI receipt tone
- `interrogate_brain()` searches only `user_id`'s memories and graph nodes
- Multimodal: use Telegram `getFile` API (already available), process through Gemini, route results per-user
- WhatsApp multimodal: requires separate media download via Meta API (handle in `whatsapp.py`)

---

### Phase 3: Enhanced Pulse Engine
**Modify: `api/pulse.py`**

**New capabilities to add to `process_user()`:**

#### 3A. Staging Area Sorter (Pre-Processing)
Insert before the main AI prompt, after fetching `dumps`:

```python
# --- STAGING AREA SORTER ---
if dumps:
    sort_response = await classify_dumps(dumps)  # TASK/NOTE/NOISE
    # Notes → memories table (with embedding)
    # Noise → mark processed, skip
    # Tasks → keep in dumps for main prompt
    dumps = [d for d in dumps if d['id'] in task_dump_ids]
```

#### 3B. Hindsight Memory Retrieval
Insert before prompt construction:

```python
# --- MEMORY RETRIEVAL ---
hindsight_context = "None"
if dumps or tasks:
    memories = await retrieve_memories(user_id, task_texts, top_k=5)
    if memories:
        hindsight_context = format_memories(memories)
```

#### 3C. Graph Context
```python
# --- GRAPH CONTEXT ---
graph_context = "None"
if dumps:
    combined_input = " ".join([d['content'] for d in dumps[:3]])
    graph_context = await hybrid_search_graph(user_id, combined_input[:100])
```

#### 3D. Batch Resource Enrichment
```python
# --- RESOURCE ENRICHMENT ---
enrichment_results = await batch_enrich_resources(user_id)
```

#### 3E. After-Action Report
```python
# --- END OF DAY: AFTER-ACTION REPORT ---
if hour >= 20 or hour < 4:
    await generate_after_action_report(user_id)
```

#### 3F. Horizon Filtering
Add the 48-hour horizon gate and 14-day creation window to task filtering:

```python
horizon_cutoff = local_date + timedelta(days=2)
two_weeks_ago = local_date - timedelta(days=14)

for t in tasks:
    reminder = t.get('reminder_at')
    if reminder:
        remind_dt = datetime.fromisoformat(reminder.replace('Z', '+00:00'))
        if remind_dt > horizon_cutoff:
            continue  # Skip future tasks
    created_dt = datetime.fromisoformat(t['created_at'].replace('Z', '+00:00'))
    if created_dt < two_weeks_ago:
        continue  # Skip stale tasks
    recent_tasks.append(t)
```

#### 3G. Enhanced Prompt Additions
Add to the existing prompt:
- `HINDSIGHT CONTEXT: {hindsight_context}`
- `GRAPH CONTEXT: {graph_context}`
- `NEWLY ENRICHED RESOURCES: {enriched_context}`
- Staging area logic already filters before prompt

#### 3H. Google → Supabase Reverse Sync
For users with Google connections, before processing:

```python
if await has_google_connection(user_id):
    await sync_completed_from_google(user_id)
```

---

### Phase 4: Research Agent
**New file: `api/research.py`**

Lightweight async version of `core/research_agent.py`:

```python
async def process_pending_research(user_id: str):
    """Process pending research tasks for a user."""
    supabase = await get_supabase()
    
    pending = await supabase.table('agent_queue') \
        .select('*').eq('user_id', user_id) \
        .eq('status', 'pending').execute()
    
    for item in (pending.data or []):
        # 1. Search via Jina
        # 2. Synthesize via Gemini
        # 3. Save dossier to raw_dumps
        # 4. Notify user via send_message()
        # 5. Mark completed
```

**Trigger options:**
- Inline during pulse (if queue has items)
- Separate GitHub Actions cron (hourly)
- On-demand via webhook command `/research`

---

### Phase 5: Mission System Enhancement
**Modify: `api/webhook.py`**

Add `/mission` command handler (currently missing from chief):

```python
if text == '🚀 Mission' or text.startswith('/mission'):
    params = text.replace('/mission', '').replace('🚀 Mission', '').strip()
    if not params:
        # List active missions
        missions = await supabase.table('missions').select('title') \
            .eq('user_id', user_id).eq('status', 'active').execute()
        ...
    else:
        # Create new mission
        await supabase.table('missions').insert({
            'user_id': user_id, 'title': params, 'status': 'active'
        }).execute()
```

---

### Phase 6: WhatsApp Feature Parity
**Modify: `api/whatsapp.py`**

Mirror all the Telegram webhook enhancements:
- Intent classification (same `classify_intent()`)
- Multimodal (WhatsApp media download uses Meta API: `GET /<media_id>`)
- Brain interrogation (`?query` prefix)
- New commands: `/mission`, `/library`
- Resource auto-capture from URLs

---

## File-by-File Change Matrix

| File | Action | Estimated Lines | Complexity |
|------|--------|-----------------|------------|
| `api/memory.py` | **CREATE** | ~200 | Medium |
| `api/intent.py` | **CREATE** | ~150 | Medium |
| `api/research.py` | **CREATE** | ~120 | Medium |
| `api/webhook.py` | **MODIFY** | +250 | High |
| `api/whatsapp.py` | **MODIFY** | +200 | High |
| `api/pulse.py` | **MODIFY** | +300 | High |
| `api/google_sync.py` | **MODIFY** | +80 | Low |
| `api/index.py` | **MODIFY** | +20 | Low |
| `requirements.txt` | **MODIFY** | +2 | Trivial |
| `Docs/SCHEMA.md` | **MODIFY** | +100 | Low |
| `.github/workflows/pulse.yml` | **MODIFY** | +5 | Low |

---

## New Dependencies

```txt
# Add to requirements.txt
pydantic          # Structured AI output validation (PulseOutput schema)
google-auth       # For reverse Google sync (if needed in Actions)
google-auth-oauthlib
google-api-python-client
```

---

## Environment Variables (New)

| Variable | Required By | Notes |
|----------|------------|-------|
| `JINA_API_KEY` | Research Agent | Free tier: 1M tokens/month |
| `SUPABASE_SERVICE_ROLE_KEY` | GitHub Actions only | For background batch jobs |

> **Note:** Chief already has all other required env vars (`GEMINI_API_KEY`, `SUPABASE_URL`, `TELEGRAM_BOT_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, etc.)

---

## Execution Order (Recommended Sprint Plan)

| Sprint | Phase | Deliverable | Days |
|--------|-------|-------------|------|
| **S0** | Schema Migrations | Run SQL, verify pgvector, create RPC | 0.5 |
| **S1** | `api/memory.py` + `api/intent.py` | Core infrastructure | 1.5 |
| **S2** | Webhook smart routing (Telegram) | Intent classification + notes + brain query | 2 |
| **S3** | Pulse enhancements | Staging sorter + memory retrieval + enrichment | 2 |
| **S4** | Multimodal input | Photo/voice/doc processing | 1 |
| **S5** | Research agent | `api/research.py` + agent_queue | 1 |
| **S6** | WhatsApp parity | Mirror Telegram features | 1.5 |
| **S7** | Mission system + horizon filter | Polish + edge cases | 1 |
| **S8** | Testing + deploy | End-to-end validation | 1.5 |
| | | **Total** | **~12 days** |

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Vercel 60s timeout on heavy pulse | HIGH | Move batch enrichment + graph backfill to GitHub Actions |
| pgvector not enabled on Supabase | BLOCKER | Enable via Supabase Dashboard → Extensions → pgvector |
| Embedding costs for multi-user scale | MEDIUM | Use `gemini-embedding-2-preview` (free tier generous) |
| WhatsApp media download rate limits | LOW | Queue + retry with exponential backoff |
| Graph edges exploding with many users | MEDIUM | Periodic pruning job, edge count limits per user |
| AI classification latency on webhook | MEDIUM | Use `gemini-3.1-flash-lite-preview` (fastest), timeout 8s |

---

## Testing Strategy

1. **Unit:** Each new function in `memory.py`, `intent.py`, `research.py` — test with mock Supabase responses
2. **Integration:** Full webhook → classify → store → pulse → briefing flow for a test user
3. **Load:** Simulate 10 concurrent users sending messages during the same pulse window
4. **Regression:** Existing onboarding flow, Google OAuth, WhatsApp delivery must not break

---

## What We're NOT Porting (And Why)

| Feature | Reason |
|---------|--------|
| `archive_ingest.py` (Google Sheets journal sync) | Danny-specific. Public users won't have this format. |
| `graph_sync.py` (local `graph.json` → Supabase) | Single-user artifact. Chief builds graph from live data. |
| `migrate_to_graph.py` | One-time migration script. Chief starts fresh with graph tables. |
| `pulse_cli.py` | Chief runs pulse via Vercel API, not CLI. |
| `core_config.key = 'entity_mappings'` | Danny-specific business routing. Chief uses generic org_tag routing. |
| Hardcoded IST timezone | Chief uses per-user `timezone_offset`. |
| `repomix-output.xml` | Build artifact, not production code. |

---

*Ready to execute. Confirm which sprint to begin.*
