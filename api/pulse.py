import os
import re
import asyncio
import httpx
import json
import hashlib
import time
import random
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
from supabase import create_async_client, AsyncClient
from .google_sync import (
    has_google_connection, sync_to_calendar, sync_to_google_tasks,
    delete_calendar_event,
)
from .memory import (
    store_memory, retrieve_hindsight, hybrid_search_graph,
    batch_enrich_resources, generate_after_action_report,
    get_embedding_async,
)
from .intent import classify_dumps_batch
from .billing import record_usage

# ─────────────────────────────────────────────
# LLM FALLBACK CHAIN CONSTANTS
# ─────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
PULSE_ENABLE_OPENROUTER_FALLBACK = os.getenv("PULSE_ENABLE_OPENROUTER_FALLBACK", "true").lower() == "true"
PULSE_HTTP_REFERER = os.getenv("PULSE_HTTP_REFERER", "https://chief-three.vercel.app")
PULSE_APP_NAME = os.getenv("PULSE_APP_NAME", "Chief")

BRIEFING_MODEL = "gemini-2.5-flash"
GEMMA_FALLBACK_MODEL = "gemma-4-31b-it"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

RETRYABLE_ERRORS = ['503', '504', '500', 'disconnected', 'timeout', 'deadline exceeded', 'unavailable', 'overloaded', 'rate limit']
NON_RETRYABLE_ERRORS = ['401', '403', '400', 'invalid']

_genai_client: genai.Client | None = None

def get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _genai_client

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY"))
    return _supabase_client


# ─────────────────────────────────────────────
# URL ENRICHMENT — Scrape og:title and og:description
# ─────────────────────────────────────────────

async def fetch_url_metadata(url: str) -> dict:
    """Extract title and description from URLs using OpenGraph meta tags."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
                "Accept": "text/html,application/xhtml+xml"
            }
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                html = response.text
                title_match = re.search(r'property=["\']og:title["\'] content=["\'](.*?)["\']', html, re.I)
                desc_match = re.search(r'property=["\']og:description["\'] content=["\'](.*?)["\']', html, re.I)
                title = title_match.group(1).strip() if title_match else "Unknown"
                description = desc_match.group(1).strip() if desc_match else ""
                title = re.sub(r'(\s\|.*|on X:|on LinkedIn:)', '', title).strip()
                return {"title": title, "description": description[:300]}
    except Exception as e:
        print(f"[URL SCRAPE ERROR] {url}: {e}")
    return {"title": "Unknown", "description": ""}


# ─────────────────────────────────────────────
# UNIFIED NOTIFICATION ROUTER
# ─────────────────────────────────────────────

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"

async def send_message(user_id: str, text: str):
    """Route a Pulse briefing to WhatsApp or Telegram based on user_id prefix."""
    # Auto-prefix bot identity
    if not text.startswith("🤖"):
        text = f"🤖 {text}"

    if user_id.startswith("wa_"):
        phone_number = user_id[3:]
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "text",
            "text": {"body": text}
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            if not res.is_success:
                print(f"[WA PULSE ERROR] User {user_id}: {res.text}")
    else:
        tg_chat_id = user_id[3:] if user_id.startswith("tg_") else user_id
        tg_url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
        async with httpx.AsyncClient(timeout=15.0) as client:
            tg_res = await client.post(tg_url, json={
                "chat_id": tg_chat_id,
                "text": text,
                "parse_mode": "Markdown"
            })
            if not tg_res.is_success:
                print(f"[TG ERROR] User {user_id}: Markdown rejected. Retrying plain.")
                await client.post(tg_url, json={"chat_id": tg_chat_id, "text": text})


# ─────────────────────────────────────────────
# TRIAL & ADMIN HELPERS
# ─────────────────────────────────────────────

async def is_trial_expired(user_id: str) -> bool:
    supabase = await get_supabase()
    response = await supabase.table('core_config').select('content').eq('user_id', user_id).eq('key', 'joined_at').limit(1).execute()
    data = response.data
    if not data:
        return False
    try:
        joined = datetime.fromisoformat(data[0]['content'].replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - joined).total_seconds() > (14 * 86400)
    except (ValueError, TypeError):
        return False

async def notify_admin(message: str):
    admin_id = os.getenv("ADMIN_CHAT_ID", "756478183")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not tg_token:
        return
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": admin_id, "text": message})


# ─────────────────────────────────────────────
# MULTI-PROVIDER LLM FALLBACK CHAIN
# ─────────────────────────────────────────────

class SimpleResponse:
    """Uniform response wrapper for non-Gemini providers."""
    def __init__(self, text: str):
        self.text = text


def normalize_mission_title(value: str) -> str:
    """Lowercase, strip, collapse punctuation — used for dedup comparison."""
    if not value or not isinstance(value, str):
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def _jitter(delay: float) -> float:
    return delay * (0.75 + random.random() * 0.5)


def parse_json_response(text: str):
    """Robust JSON parser with markdown fence stripping and extraction fallback."""
    if not text:
        raise ValueError("Empty response")
    text = re.sub(r'^```json\n?', '', text.strip())
    text = re.sub(r'\n?```$', '', text).strip()
    text = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON: {text[:100]}...")


async def _call_openrouter(prompt: str, config: dict) -> SimpleResponse:
    """Call OpenRouter API as final fallback."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": PULSE_HTTP_REFERER,
        "X-Title": PULSE_APP_NAME,
    }
    system_instruction = config.get('system_instruction') if config else None
    temperature = config.get('temperature', 0.7)
    response_mime_type = config.get('response_mime_type')
    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})
    body = {"model": OPENROUTER_MODEL, "messages": messages, "temperature": temperature}
    if response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OPENROUTER_BASE_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if 'choices' in data and data['choices']:
            return SimpleResponse(data['choices'][0]['message']['content'])
        return SimpleResponse(data.get('content', '') or json.dumps(data))


async def call_llm_with_fallback(
    prompt: str,
    model: str = None,
    config: dict = None,
    is_critical: bool = True,
    require_json: bool = False,
) -> SimpleResponse:
    """
    Multi-provider LLM call: Gemini → Gemma → OpenRouter.
    Falls back automatically on 5xx / rate-limit errors.
    """
    if model is None:
        model = BRIEFING_MODEL

    max_retries = 2 if is_critical else 1
    base_delay = 8 if is_critical else 4
    client = get_genai_client()

    providers = [
        {
            "name": "gemini",
            "fn": lambda: client.models.generate_content(
                model=model, contents=prompt, config=config or {}
            ),
        },
        {
            "name": "gemma",
            "fn": lambda: client.models.generate_content(
                model=GEMMA_FALLBACK_MODEL, contents=prompt, config=config or {}
            ),
        },
    ]
    if PULSE_ENABLE_OPENROUTER_FALLBACK and OPENROUTER_API_KEY:
        providers.append({
            "name": "openrouter",
            "fn": lambda: _call_openrouter(prompt, config or {}),
        })

    last_err = None
    for i, prov in enumerate(providers):
        for attempt in range(max_retries):
            try:
                t0 = time.time()
                fn = prov["fn"]
                # openrouter fn is a coroutine; gemini fns are sync
                if asyncio.iscoroutinefunction(fn):
                    response = await fn()
                else:
                    response = await asyncio.to_thread(fn)
                elapsed = time.time() - t0
                if require_json:
                    parse_json_response(response.text)   # validate only
                print(f"[LLM OK] provider={prov['name']} elapsed={elapsed:.1f}s")
                return response
            except Exception as e:
                err = str(e).lower()
                if any(ne in err for ne in NON_RETRYABLE_ERRORS):
                    raise
                if any(re_err in err for re_err in RETRYABLE_ERRORS) and attempt < max_retries - 1:
                    delay = _jitter(base_delay * (2 ** attempt))
                    print(f"[LLM RETRY] provider={prov['name']} attempt={attempt+1} delay={delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                print(f"[LLM FAIL] provider={prov['name']}: {err[:80]}")
                last_err = e
                break
        if i < len(providers) - 1:
            print(f"[LLM FALLBACK] → {providers[i+1]['name']}")
    raise last_err or Exception("All LLM providers failed")


# ─────────────────────────────────────────────
# GRAPH EDGE WRITING (non-blocking side-effect)
# ─────────────────────────────────────────────

async def write_graph_edges_for_task(
    user_id: str,
    task_id: int,
    task_title: str,
    project_id: int = None,
    task_description: str = None,
    people_cache: list = None,
):
    """
    After a task is saved to Supabase, create graph edges:
      task → BELONGS_TO → project
      task → INVOLVES → person (by name match in title/description)
    Non-blocking — if this fails the task is already saved, no rollback.
    """
    try:
        from .memory import ensure_graph_node, create_graph_edge
        supabase = await get_supabase()

        task_node_id = await ensure_graph_node(
            user_id, task_title, "task",
            metadata={"task_id": task_id, "project_id": project_id}
        )
        if not task_node_id:
            return

        if project_id:
            # Find or create a project node
            proj_res = await supabase.table('projects').select('name').eq('id', project_id).eq('user_id', user_id).limit(1).execute()
            if proj_res.data:
                proj_node_id = await ensure_graph_node(
                    user_id, proj_res.data[0]['name'], "project",
                    metadata={"project_id": project_id}
                )
                if proj_node_id:
                    await create_graph_edge(user_id, task_node_id, proj_node_id, "BELONGS_TO",
                                            metadata={"task_id": task_id})

        search_text = f"{task_title} {task_description or ''}".lower()
        for person in (people_cache or []):
            name = person.get('name', '')
            if name and name.lower() in search_text:
                person_node_id = await ensure_graph_node(
                    user_id, name, "person",
                    metadata={"people_name": name}
                )
                if person_node_id:
                    await create_graph_edge(user_id, task_node_id, person_node_id, "INVOLVES",
                                            metadata={"task_id": task_id})

        print(f"[GRAPH] Edges written for task {task_id}: '{task_title}'")
    except Exception as e:
        print(f"[GRAPH WARN] Edge write non-critical failure: {e}")


# ─────────────────────────────────────────────
# OUTCOME MEMORY (non-blocking side-effect)
# ─────────────────────────────────────────────

async def write_outcome_memory(user_id: str, task_title: str, project_name: str = None):
    """Record a type:outcome memory when a task is marked done."""
    try:
        label = f"Completed: {task_title}"
        if project_name:
            label += f" on {project_name}"
        await store_memory(user_id, label, memory_type="outcome")
        print(f"[MEMORY] Outcome recorded: {label[:60]}")
    except Exception as e:
        print(f"[MEMORY WARN] Outcome write non-critical failure: {e}")


# ─────────────────────────────────────────────
# PER-USER PULSE PROCESSING
# ─────────────────────────────────────────────

async def process_user(user_id: str, is_manual_test: bool):
    error_log: list[str] = []
    try:
        print(f"[PULSE START] Processing User: {user_id}")
        supabase = await get_supabase()

        # ─── ZOMBIE RECOVERY — reset dumps stuck in 'processing' >10 min ───
        try:
            ten_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            await supabase.table('raw_dumps') \
                .update({"is_processed": False}) \
                .eq('user_id', user_id) \
                .eq('is_processed', False) \
                .lt('created_at', ten_mins_ago) \
                .execute()
        except Exception as _ze:
            print(f"[ZOMBIE] Recovery skipped: {_ze}")

        if await is_trial_expired(user_id):
            print(f"[EXIT] User {user_id}: Trial Expired.")
            return

        core_response = await supabase.table('core_config').select('key, content').eq('user_id', user_id).execute()
        core = core_response.data
        if not core:
            print(f"[EXIT] User {user_id}: No configuration found.")
            return

        def c(key, default=None):
            return next((item['content'] for item in core if item['key'] == key), default)

        now = datetime.now(timezone.utc)

        # ─── TIME RESOLUTION ───
        try:
            offset_hours = float(c('timezone_offset', '0'))
        except ValueError:
            offset_hours = 0

        local_date = now + timedelta(hours=offset_hours)
        hour = local_date.hour
        day = local_date.weekday()  # 0=Monday, 6=Sunday
        schedule_row = c('pulse_schedule', '2')

        print(f"[TIME CHECK] User {user_id}: Local Hour {hour} | Schedule {schedule_row} | Offset {offset_hours}")

        # ─── SCHEDULE GATE ───
        should_pulse = is_manual_test
        if not is_manual_test:
            schedule_hours = {
                '1': [6, 10, 14, 18],
                '2': [8, 12, 16, 20],
                '3': [10, 14, 18, 22],
            }
            if hour in schedule_hours.get(schedule_row, []):
                should_pulse = True

        if not should_pulse:
            print(f"[EXIT] User {user_id}: Not scheduled for current hour.")
            return

        # ─── DATA RETRIEVAL ───
        dumps_response = await supabase.table('raw_dumps').select('id, content').eq('user_id', user_id).eq('is_processed', False).execute()
        dumps = dumps_response.data or []

        tasks_response = await supabase.table('tasks').select('id, title, priority, project_id, created_at, is_revenue_critical, deadline, reminder_at, duration_mins').eq('user_id', user_id).neq('status', 'done').neq('status', 'cancelled').execute()
        tasks = tasks_response.data or []

        people_response = await supabase.table('people').select('name, role, strategic_weight').eq('user_id', user_id).execute()
        people = people_response.data or []

        projects_response = await supabase.table('projects').select('id, name, org_tag').eq('user_id', user_id).execute()
        projects = projects_response.data or []

        season = c('current_season', 'No Goal Set')
        user_name = c('user_name', 'Leader')
        mission_mode = c('mission_mode', 'build')

        if not dumps and not tasks:
            print(f"[EXIT] User {user_id}: No data to process.")
            return

        # ─── RACE CONDITION LOCK ───
        last_pulse_str = c('last_pulse_at')
        if last_pulse_str:
            try:
                last_pulse = datetime.fromisoformat(last_pulse_str.replace('Z', '+00:00'))
                if (now - last_pulse).total_seconds() < 1800:
                    print(f"[LOCK] User {user_id}: Duplicate pulse blocked.")
                    return
            except ValueError:
                pass

        await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'last_pulse_at').execute()
        await supabase.table('core_config').insert([{'user_id': user_id, 'key': 'last_pulse_at', 'content': now.isoformat()}]).execute()

        # ─── STAGING AREA SORTER (Pre-classify dumps) ───
        if dumps:
            try:
                sort_result = await classify_dumps_batch(dumps)
                note_ids = sort_result.get("note_ids", [])
                noise_ids = sort_result.get("noise_ids", [])
                task_ids = sort_result.get("task_ids", [])

                # Route notes directly to semantic memory
                for d in dumps:
                    if d["id"] in note_ids:
                        await store_memory(user_id, d["content"], memory_type="note")

                # Mark notes + noise as processed
                skip_ids = note_ids + noise_ids
                if skip_ids:
                    await supabase.table('raw_dumps').update({'is_processed': True}).in_('id', skip_ids).execute()
                    print(f"[STAGING] user={user_id}: {len(task_ids)} tasks, {len(note_ids)} notes, {len(noise_ids)} noise")

                # Keep only task dumps for the main AI prompt
                dumps = [d for d in dumps if d["id"] in task_ids]
            except Exception as e:
                print(f"[STAGING ERROR] user={user_id}: {e}")

        # ─── BATCH RESOURCE ENRICHMENT ───
        enrichment_results = []
        try:
            enrichment_results = await batch_enrich_resources(user_id)
        except Exception as e:
            print(f"[ENRICH ERROR] user={user_id}: {e}")

        # ─── HINDSIGHT MEMORY RETRIEVAL (entity-seeded) ───
        hindsight_context = "None"
        hindsight_stale = False
        task_inputs = [d['content'] for d in dumps] if dumps else []
        try:
            if task_inputs or tasks:
                # Build entity terms from people + projects as seed signals
                entity_terms = (
                    [p.get('name', '') for p in people if p.get('name')] +
                    [p.get('name', '') for p in projects if p.get('name')]
                )
                hindsight_lines, hindsight_stale = await retrieve_hindsight(
                    user_id, task_inputs, tasks, top_k=5, entity_terms=entity_terms or None
                )
                if hindsight_lines:
                    hindsight_context = "\n".join(hindsight_lines)
                    print(f"[HINDSIGHT] user={user_id}: {len(hindsight_lines)} memories retrieved")
        except Exception as e:
            print(f"[HINDSIGHT ERROR] user={user_id}: {e}")

        # ─── GRAPH CONTEXT ───
        graph_context = "None"
        try:
            if task_inputs:
                combined_input = " ".join(task_inputs[:3])[:100]
                graph_ctx = await hybrid_search_graph(user_id, combined_input)
                if graph_ctx:
                    graph_context = graph_ctx
        except Exception as e:
            print(f"[GRAPH ERROR] user={user_id}: {e}")

        # ─── CANONICAL PAGES (synthesized project knowledge) ───
        canonical_context = "None"
        try:
            if projects:
                project_names = [p.get('name', '') for p in projects if p.get('name')]
                if project_names:
                    # Build an OR filter for title matching any active project name
                    or_parts = ",".join([f"title.ilike.%{n}%" for n in project_names[:5]])
                    pages_res = await supabase.table('canonical_pages') \
                        .select('title, content') \
                        .or_(or_parts) \
                        .limit(3) \
                        .execute()
                    if pages_res.data:
                        canonical_context = "\n\n".join(
                            f"[CANONICAL — DO NOT LIST IN BRIEFING]\n### {p['title']}\n{p['content'][:400]}"
                            for p in pages_res.data
                        )
                        print(f"[CANONICAL] user={user_id}: {len(pages_res.data)} pages loaded")
        except Exception as e:
            # Table may not exist yet — graceful skip
            print(f"[CANONICAL SKIP] user={user_id}: {e}")

        # ─── ENRICHED RESOURCES CONTEXT ───
        newly_enriched_context = "None"
        if enrichment_results:
            enriched_lines = [
                f"[{r.get('category', 'LINK')}] {r.get('strategic_note', '')}"
                for r in enrichment_results
            ]
            newly_enriched_context = " | ".join(enriched_lines)

        # ─── TIME & DAY INTELLIGENCE ───
        is_weekend = day in [5, 6]
        is_monday_morning = (day == 0 and hour < 11)

        # Mission mode shapes the AI persona
        mode_personas = {
            "fix":   "Crisis manager. Ruthlessly prioritize debt clearance and blocking issues. No fluff.",
            "grow":  "Sales strategist. Focus on revenue, leads, deals, and growth metrics.",
            "build": "Product engineer. Focus on shipping, building, and deep work blocks.",
            "rest":  "Wellbeing coach. Focus on family, health, and sustainable pace.",
        }
        mode_persona = mode_personas.get(mission_mode, mode_personas["build"])

        if is_weekend:
            briefing_mode = "WEEKEND: CHORES & IDEAS"
            system_persona = "Focus on personal tasks, family, and rest. Hide work items."
        else:
            if hour < 11:
                briefing_mode = "URGENT: CRITICAL ACTIONS"
                system_persona = f"Morning energy. {mode_persona}"
            elif hour < 15:
                briefing_mode = "IMPORTANT: STRATEGIC MOMENTUM"
                system_persona = f"Midday tactical. {mode_persona}"
            elif hour < 19:
                briefing_mode = "CHORES: WIND DOWN"
                system_persona = "Closing loops. Push to wrap up work and transition to personal time."
            else:
                briefing_mode = "IDEAS: REFLECTION"
                system_persona = "Relaxed reflection. Log ideas and observations. Prep for rest."

        # ─── TASK FILTERING (with Horizon Gate) ───
        is_overloaded = len(tasks) > 15
        horizon_cutoff = local_date + timedelta(days=2)
        two_weeks_ago = local_date - timedelta(days=14)

        filtered_tasks = []
        for t in tasks:
            # Horizon Gate: skip tasks with reminder > 48h away
            raw_reminder = t.get('deadline') or t.get('reminder_at')
            if raw_reminder:
                try:
                    remind_dt = datetime.fromisoformat(str(raw_reminder).replace(' ', 'T').replace('Z', '+00:00'))
                    if remind_dt.tzinfo is None:
                        remind_dt = remind_dt.replace(tzinfo=timezone.utc)
                    if remind_dt > horizon_cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            # Creation window: skip tasks older than 14 days (unless urgent)
            if t.get('priority', '').lower() != 'urgent':
                created_str = t.get('created_at', '')
                if created_str:
                    try:
                        created_dt = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                        if created_dt < two_weeks_ago:
                            continue
                    except (ValueError, TypeError):
                        pass

            if t.get('priority', '').lower() == 'urgent':
                filtered_tasks.append(t)
                continue
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            o_tag = project.get('org_tag', 'INBOX') if project else 'INBOX'
            if is_weekend:
                if o_tag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)
            elif hour < 19:
                filtered_tasks.append(t)
            else:
                if o_tag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)

        # ─── TASK COMPRESSION ───
        compressed_tasks = []
        for t in filtered_tasks:
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            p_name = project.get('name', 'General') if project else 'General'
            o_tag = project.get('org_tag', 'INBOX') if project else 'INBOX'
            rev = " [REV-CRITICAL]" if t.get('is_revenue_critical') else ""
            compressed_tasks.append(f"[{o_tag} >> {p_name}] {t.get('title')} ({t.get('priority', 'important')}){rev} [ID:{t.get('id')}]")

        compressed_tasks_str = ' | '.join(compressed_tasks)[:3000]
        universal_task_map = ' | '.join([f"[ID:{t.get('id')}] {t.get('title')}" for t in tasks])[:3000]

        # ─── STAGNANT TASK NAG ───
        overdue_tasks = []
        for t in filtered_tasks:
            created_str = t.get('created_at')
            if t.get('priority', '').lower() == 'urgent' and created_str:
                try:
                    created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                    if (now - created).total_seconds() / 3600 > 48:
                        overdue_tasks.append(t.get('title'))
                except ValueError:
                    pass

        # ─── STALE TASKS (7-day old todos) ───
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        stale_tasks = [
            t for t in tasks
            if t.get('status') == 'todo'
            and t.get('created_at', '') < seven_days_ago
            and t.get('title') not in overdue_tasks
        ]
        stale_tasks = sorted(stale_tasks, key=lambda t: t.get('created_at', ''))[:5]
        stale_context = None
        if stale_tasks:
            stale_lines = []
            for t in stale_tasks:
                try:
                    created = datetime.fromisoformat(t.get('created_at', '').replace('Z', '+00:00'))
                    days_old = (now - created).days
                    stale_lines.append(f"- {t.get('title', '')} (stale {days_old}d)")
                except Exception:
                    pass
            stale_context = "\n".join(stale_lines) if stale_lines else None

        # ─── PENDING EMAIL TASKS (for inline briefing section) ───
        pending_email_tasks = []
        try:
            auto_expire_cutoff = (now - timedelta(days=7)).isoformat()
            await supabase.table('email_pending_tasks') \
                .update({'danny_decision': 'expired'}) \
                .eq('user_id', user_id) \
                .is_('danny_decision', 'null') \
                .lt('created_at', auto_expire_cutoff) \
                .execute()
            email_q_res = await supabase.table('email_pending_tasks') \
                .select('id, suggested_title, suggested_project') \
                .eq('user_id', user_id) \
                .eq('shown_in_brief', False) \
                .is_('danny_decision', 'null') \
                .order('created_at', desc=False) \
                .limit(5) \
                .execute()
            pending_email_tasks = email_q_res.data or []
        except Exception as e:
            print(f"[EMAIL PENDING] user={user_id}: {e}")

        # ─── URL ENRICHMENT ───
        dumps_text = '\n---\n'.join([d.get('content', '') for d in dumps]) if dumps else 'None'
        enriched_links = []
        urls_found = re.findall(r'(https?://\S+)', dumps_text)
        for url in urls_found[:5]:  # Limit to 5 URLs to stay within timeout
            meta = await fetch_url_metadata(url)
            enriched_links.append(f"URL: {url} | Title: {meta['title']} | Snippet: {meta['description']}")
        link_context = "\n".join(enriched_links) if enriched_links else "None"

        # ─── CONTEXT STRINGS ───
        projects_names = json.dumps([p.get('name') for p in projects])
        people_names = json.dumps([p.get('name') for p in people])
        current_time_str = local_date.strftime("%A, %B %d, %Y at %I:%M %p")

        # ─── THE PROMPT ───
        prompt = f"""
        ROLE: Digital Chief of Staff for {user_name}.
        STRATEGIC CONTEXT (USER'S 14-DAY GOAL): {season}
        MISSION MODE: {mission_mode.upper()}
        CURRENT PHASE: {briefing_mode}
        CURRENT TIME: {current_time_str}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT TASKS: {json.dumps(overdue_tasks)}
        STALE_TASKS: {stale_context or 'None'}
        PERSONA GUIDELINE: {system_persona}
        HINDSIGHT_STALE: {'TRUE' if hindsight_stale else 'FALSE'}

        HINDSIGHT CONTEXT (Past lessons relevant to current inputs):
        {hindsight_context}

        KNOWLEDGE GRAPH (Relationships between entities):
        {graph_context}

        CANONICAL STRATEGIC TRUTH (synthesized project knowledge — context only, do NOT list in briefing):
        {canonical_context}

        CONTEXT:
        - PROJECTS: {projects_names}
        - PEOPLE: {people_names}
        - OPEN TASKS (FILTERED FOR TIME-OF-DAY): {compressed_tasks_str}
        - ALL TASKS (FOR COMPLETION MATCHING): {universal_task_map}
        - ENRICHED WEB LINKS: {link_context}
        - NEWLY ENRICHED RESOURCES: {newly_enriched_context}
        - NEW RAW INPUTS: {dumps_text}
        - 📧 EMAIL-SUGGESTED TASKS (surface under "📧 Inbox" section — user decides, never auto-create):
        {chr(10).join(f"- {t['suggested_title']} (Project: {t.get('suggested_project') or 'Unknown'})" for t in pending_email_tasks) if pending_email_tasks else "None"}

        INSTRUCTIONS:
        1. STRICT DATA FIDELITY: Never invent or hallucinate tasks, projects, or people.
        2. ZERO-DUMP PROTOCOL: If NEW RAW INPUTS is "None" or empty, all mutation arrays MUST be empty [].
        3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects.
        4. STRATEGIC NAG: If STAGNANT URGENT TASKS has items, start the briefing by calling them out directly.
        5. CHECK FOR COMPLETION against ALL TASKS:
            - User says finished/completed/done -> status "done"
            - User describes a result fulfilling a task objective -> "done"
            - User uses past tense of the task action verb -> "done"
            - User says cancel/ignore/forget/skip/drop -> status "cancelled"
        6. AUTO-ONBOARDING: New client/org -> "new_projects". New person mentioned -> "new_people".
        7. WEEKEND FILTER: If weekend ({is_weekend}), hide work tasks. Note any work inputs for Monday.
        8. RESOURCE CAPTURE: If NEW INPUTS contains URLs, categorize them and add to "resources" array. Do NOT create tasks from URLs unless the user explicitly says to.
        9. STALE LOOPS: If STALE_TASKS has items, include a ⏳ *STALE LOOPS* section listing them with day count. Max 5.

        10. *BRIEFING FORMAT — THIS IS CRITICAL*:
            The briefing will be sent directly via WhatsApp. It must look clean, structured, and professional.

            HEADER: Start with a one-line greeting: "*Good [morning/afternoon/evening], {user_name}* 👋" based on CURRENT TIME.
            Then a blank line.

            If STAGNANT URGENT TASKS exist, add a *🔴 OVERDUE* section first with those items.
            If STALE_TASKS exist, add a *⏳ STALE LOOPS* section listing them with days count.

            SECTION STRUCTURE (only include sections that have items):
            - Use section headers with emoji: "✅ *COMPLETED*", "💼 *WORK*", "🏠 *HOME*", "💡 *IDEAS*", "📌 *UPCOMING*"
            - Under each header, list items as: "→ Item text"
            - Add deadline/date info inline when relevant: "→ Take RE letter to RTO _(by Monday)_"
            - Add a blank line between sections.
            - Hide WORK section on weekends. Hide HOME/IDEAS sections in morning briefings unless relevant.
            - If EMAIL-SUGGESTED TASKS has items, add a "📧 *INBOX*" section. Format each as: "→ [task]. Reply to confirm or ignore."

            FOOTER: End with a short one-liner based on mission mode and time of day.

            FORMATTING RULES:
            - Use ONLY single asterisks (*bold*) for bold. NEVER use double asterisks.
            - Use underscores for italic (_italic_) ONLY for dates/notes, never for emphasis.
            - NEVER include task IDs, weights, scores, or internal metadata.
            - NEVER use markdown headers (#). Only WhatsApp-compatible formatting.
            - Keep it concise: max 3-5 items per section. No filler text.
            - Do NOT add "Reply ok" or session prompts.

        11. MARKDOWN SAFETY:
            - Use ONLY single asterisks (*) for bold.
            - Never nest formatting.
            - Arrow (→) for list items, not dashes or bullets.

        OUTPUT JSON:
        {{
            "completed_task_ids": [
                {{ "id": "123", "status": "done" }},
                {{ "id": "456", "status": "cancelled" }}
            ],
            "new_projects": [{{ "name": "...", "org_tag": "INBOX" }}],
            "new_people": [{{ "name": "...", "role": "...", "strategic_weight": 5 }}],
            "new_tasks": [{{ "title": "...", "project_name": "...", "priority": "urgent", "est_min": 15 }}],
            "resources": [{{ "url": "...", "title": "...", "summary": "...", "category": "ARTICLE" }}],
            "logs": [{{ "entry_type": "IDEAS", "content": "..." }}],
            "briefing": "The formatted briefing string for WhatsApp."
        }}
        """

        # ─── AI GENERATION (with multi-provider fallback) ───
        ai_data = {
            "briefing": f"⚠️ System busy. {len(dumps)} inputs queued.",
            "new_tasks": [], "logs": [], "completed_task_ids": [],
            "new_projects": [], "new_people": [], "resources": [],
        }
        try:
            response = await call_llm_with_fallback(
                prompt=prompt,
                model=BRIEFING_MODEL,
                config={"response_mime_type": "application/json"},
                is_critical=True,
                require_json=True,
            )
            raw_text = response.text
            clean_json = re.sub(r'^```json\n?', '', raw_text).strip()
            clean_json = re.sub(r'\n?```$', '', clean_json).strip()
            ai_data = json.loads(clean_json)
        except Exception as e:
            print(f"[AI ERROR] user={user_id}: {e}")
            error_log.append("AI generation failed")

        # ─── SEND BRIEFING ───
        if ai_data.get("briefing"):
            briefing = ai_data["briefing"].strip()
            briefing = re.sub(r'\[?ID:\s*\d+\]?', '', briefing, flags=re.IGNORECASE).strip()

            # ─── EMAIL DECISIONS inline section ───
            shown_email_ids = []
            if pending_email_tasks:
                lines = [f"\n\n📨 *INBOX* ({len(pending_email_tasks)}) — reply code yes/drop"]
                for row in pending_email_tasks:
                    shortcode = str(row['id'])[-4:]
                    proj = f" ({row['suggested_project']})" if row.get('suggested_project') else ""
                    lines.append(f"[{shortcode}] {row['suggested_title'][:60]}{proj}")
                briefing += "\n".join(lines)
                shown_email_ids = [row['id'] for row in pending_email_tasks]

            # Add Google connect nudge if not connected
            if not await has_google_connection(user_id):
                briefing += "\n\n📅 _Tip: Connect Google Calendar to auto-sync your tasks. Type *settings* to set it up._"

            # Append error summary if any pipeline failures
            if error_log:
                briefing += f"\n\n⚠️ {len(error_log)} item(s) need attention — check logs."

            await send_message(user_id, briefing)
            await record_usage(user_id, "pulse", channel="system")

            # Mark email tasks as shown after confirmed send
            if shown_email_ids:
                try:
                    await supabase.table('email_pending_tasks') \
                        .update({'shown_in_brief': True}) \
                        .in_('id', shown_email_ids) \
                        .execute()
                except Exception as e:
                    print(f"[EMAIL SHOWN] user={user_id}: {e}")

        # ─── DATABASE WRITES ───

        # Mark dumps processed
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            await supabase.table('raw_dumps').update({'is_processed': True}).in_('id', dump_ids).execute()

        # New Projects
        new_projects = ai_data.get("new_projects", [])
        if new_projects:
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH', 'QHORD', 'INBOX']
            inserts = []
            for np in new_projects:
                exists = any(
                    np.get('name', '').lower() in p.get('name', '').lower() or
                    p.get('name', '').lower() in np.get('name', '').lower()
                    for p in projects
                )
                if not exists:
                    tag = np.get('org_tag', 'INBOX')
                    if tag not in valid_tags:
                        tag = 'INBOX'
                    inserts.append({
                        'user_id': user_id,
                        'name': np.get('name', 'General'),
                        'org_tag': tag,
                        'status': 'active',
                        'context': 'personal' if tag in ['CHURCH', 'PERSONAL'] else 'work'
                    })
            if inserts:
                created = await supabase.table('projects').insert(inserts).execute()
                if created.data:
                    projects.extend(created.data)

        # New People
        new_people = ai_data.get("new_people", [])
        if new_people:
            inserts = [{
                'user_id': user_id,
                'name': p.get('name', ''),
                'role': p.get('role', ''),
                'strategic_weight': p.get('strategic_weight', 5)
            } for p in new_people]
            await supabase.table('people').insert(inserts).execute()

        # Task Completions/Cancellations
        completed = ai_data.get("completed_task_ids", [])
        new_tasks = ai_data.get("new_tasks", [])
        user_has_google = await has_google_connection(user_id) if completed or new_tasks else False

        if completed:
            for item in completed:
                target_id = item.get('id')
                status = item.get('status', 'done')
                if status not in ('done', 'cancelled'):
                    status = 'done'
                updates = {'status': status}
                if status == 'done':
                    updates['completed_at'] = now.isoformat()
                await supabase.table('tasks').update(updates).eq('id', target_id).eq('user_id', user_id).execute()

                # Outcome memory: record when task is done
                if status == 'done':
                    try:
                        task_info = await supabase.table('tasks') \
                            .select('title, project_id') \
                            .eq('id', target_id).eq('user_id', user_id).limit(1).execute()
                        if task_info.data:
                            t_title = task_info.data[0].get('title', '')
                            proj_id = task_info.data[0].get('project_id')
                            proj_name = None
                            if proj_id:
                                proj_r = await supabase.table('projects') \
                                    .select('name').eq('id', proj_id).limit(1).execute()
                                proj_name = proj_r.data[0]['name'] if proj_r.data else None
                            asyncio.create_task(
                                write_outcome_memory(user_id, t_title, proj_name)
                            )
                    except Exception as e:
                        print(f"[OUTCOME] user={user_id}: {e}")

                # Google sync: complete task + delete calendar event
                if user_has_google:
                    try:
                        task_ref = await supabase.table('tasks').select('google_task_id, google_event_id').eq('id', target_id).eq('user_id', user_id).limit(1).execute()
                        if task_ref.data:
                            g_tid = task_ref.data[0].get('google_task_id')
                            g_eid = task_ref.data[0].get('google_event_id')
                            if g_tid:
                                await sync_to_google_tasks(user_id, "", task_id=g_tid, status=status)
                            if g_eid:
                                await delete_calendar_event(user_id, g_eid)
                                await supabase.table('tasks').update({'google_event_id': None}).eq('id', target_id).eq('user_id', user_id).execute()
                    except Exception as e:
                        print(f"[GOOGLE SYNC] Completion sync failed for task {target_id}: {e}")
                        error_log.append(f"Google sync failed for task {target_id}")

        # New Tasks
        if not user_has_google and new_tasks:
            user_has_google = await has_google_connection(user_id)
        if new_tasks:
            inserts = []
            explicit_times = []
            time_slots_used: list[str] = []  # de-clash tracker

            for t in new_tasks:
                title = t.get('title', '').strip()
                if not title:
                    continue

                # ─── IDEMPOTENCY GUARD (MD5 dedup key) ───
                dedup_key = hashlib.md5(
                    f"{title.lower()}:{user_id}".encode()
                ).hexdigest()[:16]
                try:
                    existing = await supabase.table('tasks').select('id') \
                        .eq('user_id', user_id) \
                        .eq('dedup_key', dedup_key) \
                        .not_.in_('status', ['done', 'cancelled']) \
                        .limit(1).execute()
                    if existing.data:
                        print(f"[DEDUP] Skipped duplicate task: '{title}'")
                        continue
                except Exception:
                    pass  # fail open — don't block on dedup error

                ai_target = (t.get('project_name') or '').lower()
                match = next((p for p in projects if ai_target in p.get('name', '').lower() or p.get('name', '').lower() in ai_target), None)
                if not match:
                    match = next((p for p in projects if p.get('org_tag') == 'INBOX'), None)
                if not match and projects:
                    match = projects[0]

                # ─── DE-CLASH LOGIC (stagger overlapping times 15 min) ───
                raw_reminder = t.get('reminder_at') or t.get('est_start')
                sanitized_reminder = None
                explicit_time = False
                if raw_reminder:
                    sanitized_reminder = str(raw_reminder).replace(' ', 'T')
                    if 'T' in sanitized_reminder:
                        explicit_time = True
                        slot_day = sanitized_reminder.split('T')[0]
                        same_slot_count = sum(1 for s in time_slots_used if s.startswith(slot_day))
                        if same_slot_count > 0:
                            try:
                                from datetime import datetime as _dt
                                base_dt = _dt.fromisoformat(sanitized_reminder.replace('Z', '+00:00'))
                                staggered = base_dt + timedelta(minutes=15 * same_slot_count)
                                sanitized_reminder = staggered.strftime('%Y-%m-%dT%H:%M:%S') + '+05:30'
                                print(f"[DE-CLASH] '{title}' staggered to {sanitized_reminder.split('T')[1][:5]}")
                            except Exception:
                                pass
                        time_slots_used.append(sanitized_reminder.split('T')[0])

                inserts.append({
                    'user_id': user_id,
                    'title': title,
                    'project_id': match.get('id') if match else None,
                    'priority': (t.get('priority') or 'important').lower(),
                    'status': 'todo',
                    'estimated_minutes': t.get('est_min', 15),
                    'is_revenue_critical': t.get('is_revenue_critical', False),
                    'reminder_at': sanitized_reminder,
                    'dedup_key': dedup_key,
                })
                explicit_times.append(explicit_time)

            if inserts:
                result = await supabase.table('tasks').insert(inserts).execute()

                # Fire graph edge writing + Google sync as background tasks
                if result.data:
                    for created_task, expl_time in zip(result.data, explicit_times):
                        t_id = created_task.get('id')
                        t_title = created_task.get('title', '')
                        t_proj_id = created_task.get('project_id')
                        t_reminder = created_task.get('reminder_at')
                        t_priority = created_task.get('priority')

                        # Non-blocking graph edge writing
                        asyncio.create_task(
                            write_graph_edges_for_task(
                                user_id=user_id,
                                task_id=t_id,
                                task_title=t_title,
                                project_id=t_proj_id,
                                people_cache=people,
                            )
                        )

                        # Google sync: create Google Tasks + Calendar events
                        if user_has_google:
                            try:
                                g_tid = await sync_to_google_tasks(
                                    user_id, t_title, due_at=t_reminder, priority=t_priority
                                )
                                g_eid = None
                                if t_reminder and expl_time:
                                    g_eid = await sync_to_calendar(user_id, t_title, t_reminder)
                                if g_tid or g_eid:
                                    g_updates = {}
                                    if g_tid:
                                        g_updates['google_task_id'] = g_tid
                                    if g_eid:
                                        g_updates['google_event_id'] = g_eid
                                    await supabase.table('tasks').update(g_updates).eq('id', t_id).eq('user_id', user_id).execute()
                            except Exception as e:
                                print(f"[GOOGLE SYNC] New task sync failed: {e}")
                                error_log.append(f"Google sync failed for: '{t_title}'")

        # Resources (new feature)
        resources = ai_data.get("resources", [])
        if resources:
            inserts = []
            for r in resources:
                p_name = (r.get('project_name') or '').lower()
                proj_match = next((p for p in projects if p_name in p.get('name', '').lower()), None)
                inserts.append({
                    'user_id': user_id,
                    'url': r.get('url', ''),
                    'title': r.get('title', ''),
                    'summary': r.get('summary', ''),
                    'category': r.get('category', 'LINK'),
                    'project_id': proj_match.get('id') if proj_match else None,
                })
            if inserts:
                try:
                    await supabase.table('resources').insert(inserts).execute()
                    print(f"[RESOURCES] Saved {len(inserts)} resources for {user_id}")
                except Exception as e:
                    # resources table may not exist yet - graceful fallback
                    print(f"[RESOURCES SKIP] Table may not exist: {e}")

        # ─── MISSIONS RESOURCE BACKFILL ───
        # Auto-link enriched resources to active missions by keyword match
        try:
            missions_res = await supabase.table('missions') \
                .select('id, title').eq('user_id', user_id).eq('status', 'active').execute()
            active_missions = missions_res.data or []
            if active_missions:
                unlinked_res = await supabase.table('resources') \
                    .select('id, title, strategic_note') \
                    .eq('user_id', user_id) \
                    .is_('mission_id', 'null') \
                    .not_.is_('enriched_at', 'null') \
                    .limit(30).execute()
                for resource in (unlinked_res.data or []):
                    resource_text = f"{resource.get('title', '')} {resource.get('strategic_note', '')}".lower()
                    for mission in active_missions:
                        mission_keywords = mission['title'].lower().split()
                        match_score = sum(1 for kw in mission_keywords if kw in resource_text)
                        if match_score >= 2:
                            await supabase.table('resources') \
                                .update({'mission_id': mission['id']}) \
                                .eq('id', resource['id']) \
                                .eq('user_id', user_id) \
                                .execute()
                            print(f"[MISSIONS] Linked resource '{resource.get('title')}' → '{mission['title']}'")
                            break
        except Exception as e:
            print(f"[MISSIONS BACKFILL] user={user_id}: {e}")

        # New Missions
        new_missions = ai_data.get("new_missions", [])
        if new_missions:
            try:
                existing_ms = await supabase.table('missions').select('id, title') \
                    .eq('user_id', user_id).eq('status', 'active').execute()
                existing_titles = {normalize_mission_title(m['title']): m for m in (existing_ms.data or [])}
                run_dedup: set = set()
                missions_created = 0
                for mission_title in new_missions:
                    if not mission_title or not isinstance(mission_title, str):
                        continue
                    norm = normalize_mission_title(mission_title)
                    if not norm or norm in run_dedup or norm in existing_titles:
                        run_dedup.add(norm)
                        continue
                    desc = f"Auto-created by Pulse from recurring patterns on {local_date}."
                    res = await supabase.table('missions').insert({
                        'user_id': user_id,
                        'title': mission_title.strip(),
                        'status': 'active',
                        'description': desc,
                    }).execute()
                    if res.data:
                        missions_created += 1
                        run_dedup.add(norm)
                        print(f"[MISSIONS] Auto-created: {mission_title}")
                if missions_created:
                    print(f"[MISSIONS] Created {missions_created} new mission(s) for {user_id}")
            except Exception as e:
                print(f"[MISSIONS WRITE] user={user_id}: {e}")

        # Logs
        logs = ai_data.get("logs", [])
        if logs:
            inserts = [{
                'user_id': user_id,
                'entry_type': l.get('entry_type', 'LOG'),
                'content': l.get('content', '')
            } for l in logs]
            await supabase.table('logs').insert(inserts).execute()

        # ─── AFTER-ACTION REPORT (end of day) ───
        if hour >= 20 or hour < 4:
            try:
                await generate_after_action_report(user_id, local_date)
            except Exception as e:
                print(f"[AAR ERROR] user={user_id}: {e}")

    except Exception as e:
        print(f"[CRITICAL] User {user_id}: {str(e)}")
        await notify_admin(f"Pulse Failure: {user_id}\nErr: {str(e)}")


# ─────────────────────────────────────────────
# MASTER PULSE ORCHESTRATOR
# ─────────────────────────────────────────────

async def process_pulse(is_manual_test: bool):
    try:
        supabase = await get_supabase()
        response = await supabase.table('core_config').select('user_id').eq('key', 'current_season').execute()
        active_users = response.data or []

        if not active_users:
            print("No active users.")
            return

        unique_user_ids = list(set([str(u['user_id']).strip() for u in active_users]))
        print(f"[ENGINE] Found {len(unique_user_ids)} active users.")

        batch_size = 3
        for i in range(0, len(unique_user_ids), batch_size):
            batch = unique_user_ids[i:i + batch_size]
            coros = [process_user(uid, is_manual_test) for uid in batch]
            await asyncio.gather(*coros, return_exceptions=True)
            if i + batch_size < len(unique_user_ids):
                await asyncio.sleep(1)

    except Exception as e:
        print(f"Master Pulse Error: {str(e)}")
