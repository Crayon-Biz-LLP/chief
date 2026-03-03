import os
import httpx
from supabase import create_async_client, AsyncClient
from datetime import datetime, timezone
import re

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY"))
    return _supabase_client

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
        [{"text": "👥 People"}, {"text": "🔓 Vault"}],
        [{"text": "🧭 Main Goal"}, {"text": "⚙️ Settings"}]
    ],
    "resize_keyboard": True,
    "persistent": True
}

PERSONA_KEYBOARD = {
    "keyboard": [[{"text": "⚔️ Commander"}, {"text": "🏗️ Architect"}, {"text": "🌿 Nurturer"}]],
    "resize_keyboard": True,
    "one_time_keyboard": True
}

SCHEDULE_KEYBOARD = {
    "keyboard": [[{"text": "🌅 Early"}, {"text": "☀️ Standard"}, {"text": "🌙 Late"}]],
    "resize_keyboard": True,
    "one_time_keyboard": True
}

SETTINGS_KEYBOARD = {
    "keyboard": [
        [{"text": "🎭 Change Persona"}, {"text": "⏰ Change Schedule"}],
        [{"text": "📍 Change Location"}, {"text": "🎯 Change Goal"}],
        [{"text": "🔙 Back to Dashboard"}]
    ],
    "resize_keyboard": True
}

def tz_display(offset: str) -> str:
    try:
        num = float(offset)
        sign = "+" if num >= 0 else ""
        return f"🌍 **Local Sync:** GMT{sign}{num}"
    except (ValueError, TypeError):
        return f"🌍 **Local Sync:** GMT+5.5"

async def is_trial_expired(user_id: str) -> bool:
    supabase = await get_supabase()
    # joined_at is written once on /start and never updated
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

async def send_telegram(chat_id: str, text: str, reply_markup: dict = MAIN_KEYBOARD):
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": reply_markup
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def set_config(user_id: str, key: str, content: str):
    supabase = await get_supabase()
    await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', key).execute()
    await supabase.table('core_config').insert([{'user_id': user_id, 'key': key, 'content': content}]).execute()

async def process_webhook(update: dict):
    message = update.get("message")
    if not message:
        return
    
    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    text = message.get("text", "")
    
    supabase = await get_supabase()

    # --- 1. /start COMMAND ---
    if text.startswith('/start'):
        raw_name = message["from"].get("first_name", "Leader")
        first_name = re.sub(r'[*_`\[\]]', '', raw_name)

        await supabase.table('core_config').delete().eq('user_id', user_id).execute()
        await supabase.table('people').delete().eq('user_id', user_id).execute()

        # Store join timestamp once — used for 14-day trial expiry
        await set_config(user_id, 'joined_at', datetime.now(timezone.utc).isoformat())
        await set_config(user_id, 'user_name', first_name)

        welcome_msg = (
            f"🎯 **Welcome to your 14-Day Sprint, {first_name}.**\n\n"
            "I am your Digital 2iC. Let's configure your engine.\n\n"
            "**Step 1: Choose my Persona:**\n\n"
            "⚔️ **Commander:** Direct and urgent. Focuses on rapid execution.\n\n"
            "🏗️ **Architect:** Methodical and structured. Focuses on engineering systems.\n\n"
            "🌿 **Nurturer:** Balanced and proactive. Focuses on team dynamics and growth."
        )

        await send_telegram(chat_id, welcome_msg, PERSONA_KEYBOARD)
        return

    # --- 2. FETCH CURRENT STATE ---
    response = await supabase.table('core_config').select('key, content').eq('user_id', user_id).execute()
    configs = response.data or []

    identity = next((c['content'] for c in configs if c['key'] == 'identity'), None)
    schedule = next((c['content'] for c in configs if c['key'] == 'pulse_schedule'), None)
    season = next((c['content'] for c in configs if c['key'] == 'current_season'), None)
    has_people = next((c['content'] for c in configs if c['key'] == 'initial_people_setup'), None)

    # --- 3. THE ONBOARDING STATE MACHINE ---

    # Step 1: Persona
    if not identity:
        if bool(re.search(r'Commander|Architect|Nurturer', text)):
            val = '1' if 'Commander' in text else '2' if 'Architect' in text else '3'
            await set_config(user_id, 'identity', val)
            if has_people:
                await send_telegram(chat_id, "✅ **Persona Updated.**", MAIN_KEYBOARD)
                return
            
            schedule_msg = (
                "✅ **Persona locked.**\n\n**Step 2: Choose your Briefing Schedule**\nWhen do you want your Briefings?\n\n"
                "🌅 **Early:** 6AM, 10AM, 2PM, 6PM\n"
                "☀️ **Standard:** 8AM, 12PM, 4PM, 8PM\n"
                "🌙 **Late:** 10AM, 2PM, 6PM, 10PM\n\n"
                "*(Weekends are reduced to 2 Check-ins per day)*"
            )
            await send_telegram(chat_id, schedule_msg, SCHEDULE_KEYBOARD)
        else:
            await send_telegram(chat_id, "Please select a Persona to continue:", PERSONA_KEYBOARD)
        return

    # Step 2: Schedule
    if not schedule:
        if bool(re.search(r'Early|Standard|Late', text)):
            val = '1' if 'Early' in text else '2' if 'Standard' in text else '3'
            await set_config(user_id, 'pulse_schedule', val)
            if has_people:
                await send_telegram(chat_id, "✅ **Schedule Updated.**", MAIN_KEYBOARD)
                return
            
            loc_msg = (
                "✅ **Schedule locked.**\n\n**Step 3: What is your GMT/UTC Timezone Offset?**\n"
                "Type your number (e.g., `5.5` for India, `-5` for EST, `11` for Sydney, `0` for UK)."
            )
            await send_telegram(chat_id, loc_msg, {"remove_keyboard": True})
        else:
            await send_telegram(chat_id, "Please select a briefing schedule:", SCHEDULE_KEYBOARD)
        return

    # Step 3: Pure Number Timezone Resolver
    tz_offset_cfg = next((c['content'] for c in configs if c['key'] == 'timezone_offset'), None)
    if not tz_offset_cfg:
        match = re.search(r'-?\d+(\.\d+)?', text)
        if match:
            offset = match.group(0)
            await set_config(user_id, 'timezone_offset', offset)
            
            if has_people:
                sign_str = '+' if float(offset) >= 0 else ''
                await send_telegram(chat_id, f"✅ **Timezone Updated to GMT{sign_str}{offset}.**", MAIN_KEYBOARD)
                return
            
            sign_str = '+' if float(offset) >= 0 else ''
            goal_msg = (
                f"✅ **Synced to GMT{sign_str}{offset}.**\n\n**Step 4: Define your Main Goal:**\n"
                "This is the single most important outcome you are hunting for these 14 days."
            )
            await send_telegram(chat_id, goal_msg)
        else:
            await send_telegram(chat_id, "⚠️ Please enter a valid number for your offset (e.g., `-5`, `5.5`, `11`).")
        return

    # Step 4: Main Goal
    if not season:
        if text and len(text) > 5 and not text.startswith('/'):
            await set_config(user_id, 'current_season', text)
            if has_people:
                await send_telegram(chat_id, "✅ **Main Goal Updated.**", MAIN_KEYBOARD)
                return
            
            people_msg = (
                "✅ **Main Goal locked.**\n\n**Step 4: Key Stakeholders**\nWho are the top people that influence your success?\n\n"
                "*Format:* Name (Role), Name (Role)\n*Example:* Jane (Wife), John (Client Partner)\n\n"
                "*(If you prefer to add these later, just type **Skip**)*\n\n*Type them below:*"
            )
            await send_telegram(chat_id, people_msg, {"remove_keyboard": True})
        else:
            await send_telegram(chat_id, "Please define your 14-day Main Goal.", {"remove_keyboard": True})
        return

    # Step 5: Key People & Finalizing
    if not has_people:
        if text and not text.startswith('/') and text != '👥 People':
            lower_text = text.strip().lower()
            people_data = []

            if lower_text in ['skip', 'none', 'no', 'me']:
                await set_config(user_id, 'initial_people_setup', 'true')
            else:
                entries = [e.strip() for e in text.split(',')]
                for entry in entries:
                    match = re.match(r'(.*?)\((.*?)\)', entry)
                    people_data.append({
                        'user_id': user_id,
                        'name': match.group(1).strip() if match else entry,
                        'role': match.group(2).strip() if match else 'Sprint Contact',
                        'strategic_weight': 5
                    })
                
                if people_data:
                    await supabase.table('people').insert(people_data).execute()
                await set_config(user_id, 'initial_people_setup', 'true')

            persona_map = {
                '1': '⚔️ **Commander:** I will drive rapid execution, prioritizing immediate action and urgent deliverables in your briefings.',
                '2': '🏗️ **Architect:** I will engineer structured systems, breaking your raw thoughts down into methodical, scalable steps.',
                '3': '🌿 **Nurturer:** I will balance your momentum with team dynamics, focusing on sustainable growth and key relationships.'
            }

            schedule_map = {
                '1': '🌅 **Early:** Expect your briefings at 6AM, 10AM, 2PM, and 6PM.',
                '2': '☀️ **Standard:** Expect your briefings at 8AM, 12PM, 4PM, and 8PM.',
                '3': '🌙 **Late:** Expect your briefings at 10AM, 2PM, 6PM, and 10PM.'
            }

            stakeholders_display = f"{len(people_data)} key stakeholders registered." if people_data else "None registered yet. (You can add them later using /person)"
            
            tz_off = tz_offset_cfg or "5.5"
            armed_msg = (
                f"✅ **Setup Complete. Initialization Complete.**\n\n"
                f"Here is how your Digital Chief of Staff is engineered for this 14-Day Sprint:\n\n"
                f"🧠 **Your AI Persona:**\n{persona_map.get(identity, 'Default')}\n\n"
                f"⏱️ **The Check-in Schedule:**\n{schedule_map.get(schedule, 'Standard')}\n"
                f"{tz_display(tz_off)}\n"
                f"*(A \"Check-in\" is a proactive Briefing where I organize your raw thoughts into actionable tasks).* \n\n"
                f"🧭 **Your Main Goal:**\n\"{season}\"\n"
                f"*(Every idea or task you send me will be ruthlessly prioritized against this specific outcome).*\n\n"
                f"👥 **Influence Map:**\n{stakeholders_display}\n\n"
                f"🔒 **Ironclad Privacy Protocol:**\n"
                f"Your inputs are your intellectual property. Your data is stored in a secure, isolated database and is **never** used to train public AI models.\n\n"
                f"---\n"
                f"📱 **YOUR DASHBOARD (Menu Buttons):**\n"
                f"Use the keyboard below to pull data instantly outside of your scheduled Check-in:\n"
                f"• **Urgent / Brief:** Pulls your active tasks.\n"
                f"• **Vault:** Retrieves your latest captured ideas.\n"
                f"• **Main Goal / People:** Checks your current strategic context.\n\n"
                f"🔄 **Change Settings:** If your strategy shifts or you need to change your Persona/Schedule, simply type `/start` to reset your engine.\n\n"
                f"---\n"
                f"**HOW TO OPERATE:**\n"
                f"Do not worry about formatting. Treat this chat as your raw brain dump. Whenever a task, idea, or problem crosses your mind—just type it here naturally.\n\n"
                f"I will capture the chaos, engineer it into order, and serve it back to you at your next Check-in.\n\n"
                f"*Send your first raw thought below to begin:*"
            )

            await send_telegram(chat_id, armed_msg, MAIN_KEYBOARD)
        else:
            await send_telegram(chat_id, "List your stakeholders (e.g., Sunju (Wife), Christy (Client)), or type **Skip**.")
        return

    # --- 4. THE KILL SWITCH ---
    if await is_trial_expired(user_id):
        await send_telegram(chat_id, "⏳ **Your 14-Day Sprint has concluded.** Contact Danny to upgrade.")
        return

    # --- 5. COMMAND MODE ---
    final_reply = ""
    command_list = ['🔴 Urgent', '📋 Brief', '🧭 Main Goal', '🔓 Vault', '👥 People', '⚙️ Settings', '🎭 Change Persona', '⏰ Change Schedule', '📍 Change Location', '🎯 Change Goal', '🔙 Back to Dashboard']

    if text.startswith('/') or text in command_list:
        
        if text == '⚙️ Settings':
            final_reply = "⚙️ **CONTROL PANEL**\nSelect an element of your engine to recalibrate:"
            await send_telegram(chat_id, final_reply, SETTINGS_KEYBOARD)
            return
        elif text == '🎭 Change Persona':
            await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'identity').execute()
            final_reply = "Choose your new Persona:"
            await send_telegram(chat_id, final_reply, PERSONA_KEYBOARD)
            return
        elif text == '⏰ Change Schedule':
            await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'pulse_schedule').execute()
            final_reply = "Choose your new Briefing Schedule:"
            await send_telegram(chat_id, final_reply, SCHEDULE_KEYBOARD)
            return
        elif text == '📍 Change Location':
            await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'timezone_offset').execute()
            final_reply = "Where are you located? (Enter city name):"
            await send_telegram(chat_id, final_reply, {"remove_keyboard": True})
            return
        elif text == '🎯 Change Goal':
            await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', 'current_season').execute()
            final_reply = "What is your new Main Goal for this sprint?"
            await send_telegram(chat_id, final_reply, {"remove_keyboard": True})
            return
        elif text == '🔙 Back to Dashboard':
            final_reply = "Returning to Dashboard."
            await send_telegram(chat_id, final_reply, MAIN_KEYBOARD)
            return
        
        elif text == '🔓 Vault':
            response = await supabase.table('logs').select('content, created_at').eq('user_id', user_id).ilike('entry_type', '%IDEAS%').order('created_at', desc=True).limit(5).execute()
            ideas = response.data
            if ideas:
                formatted_ideas = "\n\n".join([f"💡 *{datetime.fromisoformat(i['created_at'].replace('Z','+00:00')).strftime('%m/%d/%Y')}:* {i['content']}" for i in ideas])
                final_reply = "🔓 **THE IDEA VAULT (Last 5):**\n\n" + formatted_ideas
            else:
                final_reply = "The Vault is empty."
        
        elif text == '🧭 Main Goal':
            final_reply = f"🧭 **CURRENT MAIN GOAL:**\n\n{season}"
        
        elif text == '🔴 Urgent':
            response = await supabase.table('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').eq('user_id', user_id).limit(1).execute()
            fire = response.data[0] if response.data else None
            final_reply = f"🔴 **ACTION REQUIRED:**\n\n🔥 {fire['title']}" if fire else "✅ No active fires."
            
        elif text == '📋 Brief':
            response = await supabase.table('tasks').select('title, priority').eq('status', 'todo').eq('user_id', user_id).limit(10).execute()
            tasks = response.data or []
            if tasks:
                sorted_tasks = sorted(tasks, key=lambda t: -1 if t['priority'] == 'urgent' else 1)[:5]
                formatted_tasks = "\n".join([f"{'🔴' if t['priority'] == 'urgent' else '⚪'} {t['title']}" for t in sorted_tasks])
                final_reply = "📋 **EXECUTIVE BRIEF:**\n\n" + formatted_tasks
            else:
                final_reply = "The list is empty."
                
        elif text == '👥 People':
            response = await supabase.table('people').select('name, role').eq('user_id', user_id).execute()
            people = response.data or []
            final_reply = "👥 **STAKEHOLDERS:**\n\n" + "\n".join([f"• {p['name']} ({p['role']})" for p in people]) if people else "No one registered."
            
        elif text.startswith('/person '):
            input_str = text.replace('/person ', '').strip()
            parts = [s.strip() for s in input_str.split('|')]
            name = parts[0] if len(parts) > 0 else ""
            weight = parts[1] if len(parts) > 1 else "5"
            
            if name:
                weight_int = int(weight) if weight.isdigit() else 5
                try:
                    await supabase.table('people').insert([{'user_id': user_id, 'name': name, 'strategic_weight': weight_int}]).execute()
                    final_reply = f"👤 **Stakeholder Registered:** {name}\nStrategic Weight: {weight_int}/10"
                except Exception:
                    final_reply = "❌ Error adding person."
            else:
                final_reply = "❌ Format: `/person Name | Weight`"

        if final_reply:
            await send_telegram(chat_id, final_reply)
        return

    # --- 6. CAPTURE MODE ---
    if text:
        await supabase.table('raw_dumps').insert([{'user_id': user_id, 'content': text, 'source': 'telegram'}]).execute()
        await send_telegram(chat_id, '✅')
