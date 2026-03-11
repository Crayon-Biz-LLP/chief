import os
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
# Unified Notification Router
# ─────────────────────────────────────────────

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"

async def send_message(user_id: str, text: str):
    """Route a Pulse briefing to either Telegram or WhatsApp based on user_id prefix."""
    if user_id.startswith("wa_"):
        phone_number = user_id[3:]  # strip 'wa_'
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        # Append reply prompt to keep 24-hour window rolling
        wa_text = text + "\n\n_Reply 'ok' to confirm receipt._"
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
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers)
            if not res.is_success:
                print(f"[WA PULSE ERROR] User {user_id}: {res.text}")
    else:
        # Treat as Telegram — user_id IS the chat_id (no prefix stripping for legacy Telegram users)
        tg_chat_id = user_id[3:] if user_id.startswith("tg_") else user_id
        tg_url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
        async with httpx.AsyncClient() as client:
            tg_res = await client.post(tg_url, json={
                "chat_id": tg_chat_id,
                "text": text,
                "parse_mode": "Markdown"
            })
            if not tg_res.is_success:
                print(f"[TG ERROR] User {user_id}: Markdown rejected. Retrying plain text.")
                await client.post(tg_url, json={"chat_id": tg_chat_id, "text": text})

async def is_trial_expired(user_id: str) -> bool:
    supabase = await get_supabase()
    # joined_at is written once on /start or Initialize and never updated
    response = await supabase.table('core_config').select('content').eq('user_id', user_id).eq('key', 'joined_at').limit(1).execute()
    data = response.data
    if not data:
        return False
    
    joined_str = data[0]['content'].replace('Z', '+00:00')
    try:
        joined_at = datetime.fromisoformat(joined_str)
    except ValueError:
        return False
        
    fourteen_days_seconds = 14 * 24 * 60 * 60
    return (datetime.now(timezone.utc) - joined_at).total_seconds() > fourteen_days_seconds

async def notify_admin(message: str):
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    payload = {
        "chat_id": "756478183",
        "text": message
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

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

        now = datetime.now(timezone.utc)
        user_offset = next((c['content'] for c in core if c['key'] == 'timezone_offset'), '5.5')
        try:
            offset_hours = float(user_offset)
        except ValueError:
            offset_hours = 5.5
            
        local_date = now + timedelta(hours=offset_hours)
        hour = local_date.hour
        day = local_date.weekday() # 0 = Monday, 6 = Sunday
        schedule_row = next((c['content'] for c in core if c['key'] == 'pulse_schedule'), '2')

        print(f"[TIME CHECK] User {user_id}: Local Hour {hour} | Schedule {schedule_row} | Offset {user_offset}")

        should_pulse = is_manual_test
        if not is_manual_test:
            def check_hour(target_hours):
                return hour in target_hours
            
            if schedule_row == '1' and check_hour([6, 10, 14, 18]): should_pulse = True
            if schedule_row == '2' and check_hour([8, 12, 16, 20]): should_pulse = True
            if schedule_row == '3' and check_hour([10, 14, 18, 22]): should_pulse = True

        if not should_pulse:
            print(f"[EXIT] User {user_id}: Not scheduled for current hour.")
            return

        # Data Retrieval
        dumps_response = await supabase.table('raw_dumps').select('id, content').eq('user_id', user_id).eq('is_processed', False).execute()
        dumps = dumps_response.data or []
        
        tasks_response = await supabase.table('tasks').select('id, title, priority, project_id, created_at').eq('user_id', user_id).neq('status', 'done').neq('status', 'cancelled').execute()
        tasks = tasks_response.data or []
        
        people_response = await supabase.table('people').select('name, role, strategic_weight').eq('user_id', user_id).execute()
        people = people_response.data or []
        
        projects_response = await supabase.table('projects').select('id, name, org_tag').eq('user_id', user_id).execute()
        projects = projects_response.data or []

        season = next((c['content'] for c in core if c['key'] == 'current_season'), 'No Goal Set')
        user_name = next((c['content'] for c in core if c['key'] == 'user_name'), 'Leader')

        if not dumps and not tasks:
            print(f"[EXIT] User {user_id}: No active data to pulse.")
            return

        # ─── START OF PRODUCTION FIX: RACE CONDITION LOCK ───
        last_pulse_str = next((c['content'] for c in core if c['key'] == 'last_pulse_at'), None)
        if last_pulse_str:
            try:
                last_pulse = datetime.fromisoformat(last_pulse_str.replace('Z', '+00:00'))
                # If a pulse was triggered in the last 30 minutes, block this duplicate execution
                if (now - last_pulse).total_seconds() < 1800:
                    print(f"[LOCK] User {user_id}: Blocked duplicate pulse execution. Already fired recently.")
                    return
            except ValueError:
                pass

        # Immediately lock the database BEFORE calling Gemini
        await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'last_pulse_at').execute()
        await supabase.table('core_config').insert([{'user_id': user_id, 'key': 'last_pulse_at', 'content': now.isoformat()}]).execute()
        # ─── END OF PRODUCTION FIX ───

        # --- 🕒 UNIFIED TIME & DAY INTELLIGENCE ---
        is_weekend = day in [5, 6]
        is_monday_morning = (day == 0 and hour < 11)
        
        if is_weekend:
            briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)"
            system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed."
        else:
            if hour < 11:
                briefing_mode = "🔴 URGENT: CRITICAL ACTIONS"
                system_persona = "High-energy. Direct focus toward URGENT tasks and high-stakes 'Battlefield' items."
            elif hour < 15:
                briefing_mode = "🟡 IMPORTANT: STRATEGIC MOMENTUM"
                system_persona = "Tactical update. Focus on IMPORTANT tasks, scaling, and growth projects."
            elif hour < 19:
                briefing_mode = "⚪ CHORES: OPERATIONAL SHUTDOWN"
                system_persona = "Shutdown mode. Push user to close work loops and transition to Father/Family mode."
            else:
                briefing_mode = "💡 IDEAS: MENTAL CLEAR-OUT"
                system_persona = "Relaxed reflection. Focus on logging IDEAS and observations. Prep for sleep."

        # --- BANDWIDTH & BUFFER CHECK ---
        is_overloaded = len(tasks) > 15
        
        # --- STRATEGIC TASK FILTERING ---
        filtered_tasks = []
        for t in tasks:
            priority = t.get('priority', '').lower()
            if priority == 'urgent':
                filtered_tasks.append(t)
                continue
            
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            oTag = project.get('org_tag') if project else 'INBOX'
            
            if is_weekend:
                if oTag in ['PERSONAL', 'CHURCH']:
                    filtered_tasks.append(t)
            else:
                if hour < 19:
                    if oTag in ['SOLVSTRAT', 'PRODUCT_LABS', 'CRAYON', 'INBOX']:
                        filtered_tasks.append(t)
                else:
                    if oTag in ['PERSONAL', 'CHURCH']:
                        filtered_tasks.append(t)

        compressed_tasks = []
        for t in filtered_tasks:
            project = next((p for p in projects if p.get('id') == t.get('project_id')), None)
            pName = project.get('name', 'General') if project else 'General'
            oTag = project.get('org_tag', 'INBOX') if project else 'INBOX'
            priority = t.get('priority', 'important')
            t_id = t.get('id')
            compressed_tasks.append(f"[{oTag} >> {pName}] {t.get('title')} ({priority}) [ID:{t_id}]")
            
        compressed_tasks_str = ' | '.join(compressed_tasks)[:3000]

        # --- THE NAG LOGIC (STAGNANT TASK GUARD) ---
        overdue_tasks = []
        for t in filtered_tasks:
            created_at_str = t.get('created_at')
            priority = t.get('priority', '').lower()
            if priority == 'urgent' and created_at_str:
                try:
                    created_date = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                    if (now - created_date).total_seconds() / 3600 > 48:
                        overdue_tasks.append(t.get('title'))
                except ValueError:
                    pass

        dumps_text = '\n---\n'.join([d.get('content', '') for d in dumps]) if dumps else 'None'
        projects_names = json.dumps([p.get('name') for p in projects])
        people_names = json.dumps([p.get('name') for p in people])
        core_str = json.dumps([{c['key']: c['content']} for c in core])

        prompt = f"""
        ROLE: Digital 2iC for {user_name}.
        STRATEGIC CONTEXT: {season}
        CURRENT PHASE: {briefing_mode}
        SYSTEM_LOAD: {'OVERLOADED' if is_overloaded else 'OPTIMAL'}
        MONDAY_REENTRY: {'TRUE' if is_monday_morning else 'FALSE'}
        STAGNANT URGENT_TASKS: {json.dumps(overdue_tasks)}
        PERSONA GUIDELINE: {system_persona}
        CONTEXT:
        - IDENTITY: {core_str}
        - PROJECTS: {projects_names}
        - PEOPLE: {people_names}
        - CURRENT OPEN TASKS (COMPRESSED): {compressed_tasks_str}
        - NEW INPUTS: {dumps_text}

        / --- NEW: PROJECT ROUTING LOGIC ---
        // Use this hierarchy to assign NEW_TASKS or match COMPLETIONS:
        1. SOLVSTRAT (CASH ENGINE): Match tasks for Atna.ai, Smudge, new Lead Gen here or new SaaS and technology projects. Goal: High-ticket revenue.
        2. PRODUCT LABS (INCUBATOR): 
            - Match existing: CashFlow+ (Vasuuli), Integrated-OS.
            - Match NEW IDEAS: If the input involves "SaaS research," "New Product concept," "MVPs," or "Validation" that is NOT for a current Solvstrat client, tag as PRODUCT LABS.
            - Goal: Future equity and passive income.
        3. CRAYON (UMBRELLA): Match Governance, Tax, and Legal here.
        4. PERSONAL: Match Sunju, kids, dogs here.
        5. CHURCH: 
            - Note: All church-related activities must map to the project "Church".

        NEW PROJECT CREATION CRITERIA:
        1. Only add to "new_projects" if a COMPLETELY UNKNOWN client or organization is mentioned 

        INSTRUCTIONS:
        1. STRICT DATA FIDELITY: You are strictly forbidden from inventing, hallucinating, or generating new tasks, projects, or people. 
        2. ZERO-DUMP PROTOCOL: If NEW INPUTS is empty or "None", the "new_tasks", "new_projects", and "new_people" arrays MUST remain 100% empty [].
        3. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects. Use the ROUTING LOGIC to categorize completions and new tasks.
        4. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out. Ask why these ₹30L velocity blockers are stalled.
        5. CHECK FOR COMPLETION: Compare inputs against OPEN TASKS to identify IDs finished.
            - If {user_name} says he finished or completed a task, mark it as done.
            - If {user_name} describes a result that fulfills a task's objective, mark it DONE.
            - If {user_name} uses the past tense of a task's core action verb, mark it DONE.
            - If the input describes the final step of a process, mark it DONE.
            - If {user_name} says "Cancel", "Ignore", "Forget", or "Not doing" a task, mark status as cancelled.
            - If {user_name} indicates he is "skipping," "dropping," or "not doing" something, set status to cancelled.
        6. AUTO-ONBOARDING:
            - If a new Client/Project is mentioned, add to "new_projects".
            - If a new Person is mentioned, add to "new_people".
        7. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
        8. WEEKEND FILTER: If is_weekend is true ({is_weekend}), do NOT suggest or list Work tasks. Move work inputs to a 'Monday' reminder.
        9. EXECUTIVE BRIEF FORMAT:
            - HEADLINE RULE: Use exactly "{briefing_mode}".
            - ICON RULES: 🔴 (URGENT), 🟡 (IMPORTANT), ⚪ (CHORES), 💡 (IDEAS).
            - SECTIONS: ✅ COMPLETED, 🛡️ WORK (Hide on weekends), 🏠 HOME, 💡 IDEAS (Only at night pulse).
            - STRICT TASK SYNTAX: Every single task listed in the briefing MUST follow this exact format: "- [ICON] [Task Title]". 
            - NEGATIVE CONSTRAINTS: NEVER include task numbers, IDs, weights, scores, parentheses, or metadata in the briefing string. NEVER mention "Monday" unless it is actually the weekend.
        10. **CRITICAL MARKDOWN SAFETY**: 
            - Use ONLY single asterisks (*) for bold. 
            - Never use underscores (_) as they cause parsing errors in Telegram/Whatsapp.
            - Do not use nested formatting (e.g., no bold inside italics).
            - Ensure every opening asterisk has a matching closing asterisk.
            
        OUTPUT JSON:
        {{
            "completed_task_ids": [
                {{ "id": "123", "status": "done" }},
                {{ "id": "456", "status": "cancelled" }}
            ],
            "new_projects": [{{ "name": "...", "importance": 8, "org_tag": "SOLVSTRAT" }}],
            "new_people": [{{ "name": "...", "role": "...", "strategic_weight": 9 }}],
            "new_tasks": [{{ "title": "...", "project_name": "...", "priority": "urgent", "est_min": 15 }}],
            "logs": [{{ "entry_type": "IDEAS", "content": "..." }}],
            "briefing": "The Clean Markdown string."
        }}
        """

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
            
        if ai_data.get("briefing"):
            briefing = ai_data["briefing"].strip()
            # Clean out any leftover IDs
            import re
            briefing = re.sub(r'\[?ID:\s*\d+\]?', '', briefing, flags=re.IGNORECASE).strip()
            await send_message(user_id, briefing)

        # Database Updates
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            await supabase.table('raw_dumps').update({'is_processed': True}).in_('id', dump_ids).execute()
            
        # PROJECT CREATION
        new_projects = ai_data.get("new_projects", [])
        if new_projects:
            valid_tags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH']
            filtered_new_projects = []
            for newP in new_projects:
                already_exists = any(
                    newP.get('name', '').lower() in p.get('name', '').lower() or
                    p.get('name', '').lower() in newP.get('name', '').lower()
                    for p in projects
                )
                if not already_exists:
                    filtered_new_projects.append(newP)

            if filtered_new_projects:
                project_inserts = []
                for p in filtered_new_projects:
                    org_tag = p.get('org_tag', 'INBOX')
                    if org_tag not in valid_tags:
                        org_tag = 'INBOX'
                    context_val = 'personal' if org_tag in ['CHURCH', 'PERSONAL'] else 'work'
                    project_inserts.append({
                        'user_id': user_id,
                        'name': p.get('name', 'General'),
                        'org_tag': org_tag,
                        'status': 'active',
                        'context': context_val
                    })
                created_projects = await supabase.table('projects').insert(project_inserts).execute()
                if created_projects.data:
                    projects.extend(created_projects.data)

        # PEOPLE CREATION
        new_people = ai_data.get("new_people", [])
        if new_people:
            people_inserts = []
            for p in new_people:
                people_inserts.append({
                    'user_id': user_id,
                    'name': p.get('name', ''),
                    'role': p.get('role', ''),
                    'strategic_weight': p.get('strategic_weight', 5)
                })
            await supabase.table('people').insert(people_inserts).execute()

        # TASK UPDATES (COMPLETION/CANCELLATION)
        completed_task_ids = ai_data.get("completed_task_ids", [])
        if completed_task_ids:
            for item in completed_task_ids:
                target_id = item.get('id')
                target_status = item.get('status', 'done')
                if target_status not in ['done', 'cancelled']:
                    target_status = 'done'
                updates = {'status': target_status}
                if target_status == 'done':
                    updates['completed_at'] = now.isoformat()
                await supabase.table('tasks').update(updates).eq('id', target_id).eq('user_id', user_id).execute()

        # NEW TASKS
        new_tasks = ai_data.get("new_tasks", [])
        if new_tasks:
            task_inserts = []
            for t in new_tasks:
                ai_target = t.get('project_name', '').lower()
                target_project = next((p for p in projects if ai_target in p.get('name', '').lower() or p.get('name', '').lower() in ai_target), None)
                if not target_project:
                    target_project = next((p for p in projects if p.get('org_tag') == 'INBOX'), None)
                if not target_project and projects:
                    target_project = projects[0]
                
                project_id = target_project.get('id') if target_project else None
                task_inserts.append({
                    'user_id': user_id,
                    'title': t.get('title', ''),
                    'project_id': project_id,
                    'priority': t.get('priority', 'important').lower(),
                    'status': 'todo',
                    'estimated_minutes': t.get('est_min', 15)
                })
            if task_inserts:
                await supabase.table('tasks').insert(task_inserts).execute()
                
        # LOGS
        logs = ai_data.get("logs", [])
        if logs:
            log_inserts = []
            for l in logs:
                log_inserts.append({
                    'user_id': user_id,
                    'entry_type': l.get('entry_type', 'LOG'),
                    'content': l.get('content', '')
                })
            await supabase.table('logs').insert(log_inserts).execute()

    except Exception as e:
        print(f"[CRITICAL] User {user_id}: {str(e)}")
        await notify_admin(f"🚨 Pulse Failure: {user_id}\nErr: {str(e)}")


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
            tasks = [process_user(uid, is_manual_test) for uid in batch]
            
            await asyncio.gather(*tasks, return_exceptions=True)
            
            if i + batch_size < len(unique_user_ids):
                await asyncio.sleep(1)

    except Exception as e:
        print(f"Master Pulse Error: {str(e)}")
