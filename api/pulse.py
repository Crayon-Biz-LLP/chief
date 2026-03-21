import os
import re
import asyncio
import httpx
import json
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
from supabase import create_async_client, AsyncClient

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
    if user_id.startswith("wa_"):
        phone_number = user_id[3:]
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        wa_text = text + "\n\n_Reply 'ok' to keep your session active._"
        url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "text",
            "text": {"body": wa_text}
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
# PER-USER PULSE PROCESSING
# ─────────────────────────────────────────────

async def process_user(user_id: str, is_manual_test: bool):
    try:
        print(f"[PULSE START] Processing User: {user_id}")
        supabase = await get_supabase()

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

        tasks_response = await supabase.table('tasks').select('id, title, priority, project_id, created_at, is_revenue_critical, deadline').eq('user_id', user_id).neq('status', 'done').neq('status', 'cancelled').execute()
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

        # ─── TASK FILTERING ───
        is_overloaded = len(tasks) > 15

        filtered_tasks = []
        for t in tasks:
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
        PERSONA GUIDELINE: {system_persona}

        CONTEXT:
        - PROJECTS: {projects_names}
        - PEOPLE: {people_names}
        - OPEN TASKS (FILTERED FOR TIME-OF-DAY): {compressed_tasks_str}
        - ALL TASKS (FOR COMPLETION MATCHING): {universal_task_map}
        - ENRICHED WEB LINKS: {link_context}
        - NEW RAW INPUTS: {dumps_text}

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
        9. EXECUTIVE BRIEF FORMAT:
            - Use *bold* for headers (WhatsApp compatible).
            - SECTIONS: COMPLETED, WORK (hide weekends), HOME, IDEAS (evening only).
            - Every task: "- [Task Title]" (no IDs, no metadata).
            - Keep it concise. Max 3-5 items per section.
            - NEVER include task IDs, weights, or scores in the briefing text.
        10. MARKDOWN SAFETY:
            - Use ONLY single asterisks (*) for bold.
            - Never use underscores for emphasis (breaks WhatsApp).
            - Never nest formatting.

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
            "briefing": "The formatted briefing string for WhatsApp/Telegram."
        }}
        """

        # ─── AI GENERATION ───
        client = get_genai_client()
        result = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        raw_text = result.text
        clean_json = raw_text.replace('```json', '').replace('```', '').strip()

        try:
            ai_data = json.loads(clean_json)
        except json.JSONDecodeError:
            print(f"[JSON ERROR] Could not parse for user {user_id}")
            return

        # ─── SEND BRIEFING ───
        if ai_data.get("briefing"):
            briefing = ai_data["briefing"].strip()
            briefing = re.sub(r'\[?ID:\s*\d+\]?', '', briefing, flags=re.IGNORECASE).strip()
            await send_message(user_id, briefing)

        # ─── DATABASE WRITES ───

        # Mark dumps processed
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            await supabase.table('raw_dumps').update({'is_processed': True}).in_('id', dump_ids).execute()

        # New Projects
        new_projects = ai_data.get("new_projects", [])
        if new_projects:
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH']
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

        # New Tasks
        new_tasks = ai_data.get("new_tasks", [])
        if new_tasks:
            inserts = []
            for t in new_tasks:
                ai_target = (t.get('project_name') or '').lower()
                match = next((p for p in projects if ai_target in p.get('name', '').lower() or p.get('name', '').lower() in ai_target), None)
                if not match:
                    match = next((p for p in projects if p.get('org_tag') == 'INBOX'), None)
                if not match and projects:
                    match = projects[0]

                inserts.append({
                    'user_id': user_id,
                    'title': t.get('title', ''),
                    'project_id': match.get('id') if match else None,
                    'priority': (t.get('priority') or 'important').lower(),
                    'status': 'todo',
                    'estimated_minutes': t.get('est_min', 15),
                    'is_revenue_critical': t.get('is_revenue_critical', False),
                })
            if inserts:
                await supabase.table('tasks').insert(inserts).execute()

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

        # Logs
        logs = ai_data.get("logs", [])
        if logs:
            inserts = [{
                'user_id': user_id,
                'entry_type': l.get('entry_type', 'LOG'),
                'content': l.get('content', '')
            } for l in logs]
            await supabase.table('logs').insert(inserts).execute()

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
