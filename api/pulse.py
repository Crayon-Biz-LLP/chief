import os
import asyncio
import httpx
import json
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types

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

async def is_trial_expired(user_id: str) -> bool:
    supabase = await get_supabase()
    response = await supabase.table('core_config').select('created_at').eq('user_id', user_id).order('created_at', desc=False).limit(1).execute()
    data = response.data
    if not data:
        return False
    
    created_str = data[0]['created_at'].replace('Z', '+00:00')
    try:
        created_at = datetime.fromisoformat(created_str)
    except ValueError:
        return False
        
    fourteen_days_seconds = 14 * 24 * 60 * 60
    return (datetime.now(timezone.utc) - created_at).total_seconds() > fourteen_days_seconds

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
        
        tasks_response = await supabase.table('tasks').select('id, title, priority').eq('user_id', user_id).neq('status', 'done').neq('status', 'cancelled').execute()
        tasks = tasks_response.data or []
        
        people_response = await supabase.table('people').select('name, role').eq('user_id', user_id).execute()
        people = people_response.data or []
        
        season = next((c['content'] for c in core if c['key'] == 'current_season'), 'No Goal Set')
        user_name = next((c['content'] for c in core if c['key'] == 'user_name'), 'Leader')

        if not dumps and not tasks:
            print(f"[EXIT] User {user_id}: No active data to pulse.")
            return

        dumps_text = '\n---\n'.join([d['content'] for d in dumps]) if dumps else 'None'

        prompt = f"""
ROLE: Digital 2iC for {user_name}.
Main Goal: {season}
STAKEHOLDERS: {json.dumps(people)}
ACTIVE TASKS: {json.dumps(tasks)}
NEW INPUTS: {dumps_text}

INSTRUCTIONS:
1. Address {user_name} personally.
2. Use a high-density, scannable Markdown format. No long paragraphs.
3. **CRITICAL MARKDOWN SAFETY**: 
    - Use ONLY single asterisks (*) for bold. 
    - Never use underscores (_) as they cause parsing errors.
    - Do not use nested formatting (e.g., no bold inside italics).
    - Ensure every opening asterisk has a matching closing asterisk.    
4. Structure: 
    - [Emoji] [PULSE NAME]: [TIME-STAMP/TRIGGER NAME]
    - Personal Greeting + Progress Tracker.
    - 1-2 sharp, direct sentences from the Persona (Commander: Urgent/Aggressive | Architect: Systems/Logic | Nurturer: Balanced/Relationship-focused).
    - CATEGORIZED LISTS: (Work, Home, Ideas). 
    - Use 🔴 for Urgent, 🟡 for Important, ⚪ for Chore/Idea.
5. Prioritize tasks involving stakeholders based on their roles.
6. NEVER display Task IDs to the user. Keep the text clean.
7. If new tasks are identified in the inputs, add them to the new_tasks array.
8. SEMANTIC MATCHING: If the user's input indicates they finished or closed a task, find its 'id' in the ACTIVE TASKS list and add it to the "completed_task_ids" array.

OUTPUT JSON:
{{
    "new_tasks": [{{"title": "", "priority": "urgent/important/chore"}}],
    "completed_task_ids": [],
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
            tg_url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
            
            async with httpx.AsyncClient() as client:
                tg_res = await client.post(tg_url, json={
                    "chat_id": user_id, 
                    "text": ai_data["briefing"], 
                    "parse_mode": "Markdown"
                })
                
                if not tg_res.is_success:
                    print(f"[TG ERROR] User {user_id}: Markdown rejected. Retrying plain text.")
                    await client.post(tg_url, json={
                        "chat_id": user_id, 
                        "text": ai_data["briefing"]
                    })

        # Database Updates
        if dumps:
            dump_ids = [d['id'] for d in dumps]
            await supabase.table('raw_dumps').update({'is_processed': True}).in_('id', dump_ids).execute()
            
        new_tasks = ai_data.get("new_tasks", [])
        if new_tasks:
            task_inserts = [{'user_id': user_id, 'title': t['title'], 'priority': t.get('priority', 'chore'), 'status': 'todo'} for t in new_tasks]
            await supabase.table('tasks').insert(task_inserts).execute()
            
        completed_task_ids = ai_data.get("completed_task_ids", [])
        if completed_task_ids:
            await supabase.table('tasks').update({'status': 'done'}).in_('id', completed_task_ids).eq('user_id', user_id).execute()

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
