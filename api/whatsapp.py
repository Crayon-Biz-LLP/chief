import os
import httpx
from supabase import create_async_client, AsyncClient

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY"))
    return _supabase_client

# ─────────────────────────────────────────────
# WhatsApp API Send Helpers
# ─────────────────────────────────────────────

async def _wa_post(phone_number_id: str, payload: dict):
    """Base function to POST to WhatsApp API."""
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if not response.is_success:
            print(f"[WA ERROR] {response.status_code}: {response.text}")
        return response

async def send_whatsapp_text(phone_number_id: str, to: str, text: str):
    """Send a plain text message."""
    await _wa_post(phone_number_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    })

async def send_interactive_buttons(phone_number_id: str, to: str, body_text: str, buttons: list):
    """
    Send a message with up to 3 reply buttons.
    buttons = [{"id": "commander", "title": "⚔️ BOSS"}]
    """
    await _wa_post(phone_number_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            }
        }
    })

async def send_list_message(phone_number_id: str, to: str, body_text: str, button_label: str, rows: list):
    """
    Send a list message (bottom-sheet picker) for 4+ options.
    rows = [{"id": "fix", "title": "🛡️ FIX", "description": "Clear backlog and stop fires."}]
    """
    await _wa_post(phone_number_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_label,
                "sections": [{
                    "title": "Select an option",
                    "rows": rows
                }]
            }
        }
    })

# ─────────────────────────────────────────────
# Supabase Config Helpers
# ─────────────────────────────────────────────

async def set_config(user_id: str, key: str, content: str):
    supabase = await get_supabase()
    await supabase.table('core_config').delete().eq('user_id', user_id).eq('key', key).execute()
    await supabase.table('core_config').insert([{'user_id': user_id, 'key': key, 'content': content}]).execute()

async def get_configs(user_id: str) -> list:
    supabase = await get_supabase()
    response = await supabase.table('core_config').select('key, content').eq('user_id', user_id).execute()
    return response.data or []

# ─────────────────────────────────────────────
# Step Helpers: Send Each Onboarding Prompt
# ─────────────────────────────────────────────

async def send_step1_persona(pid: str, to: str):
    await send_interactive_buttons(pid, to,
        body_text="Welcome to your Digital 2iC. I'm here to clear your mental clutter.\n\nFirst, how should I talk to you?",
        buttons=[
            {"id": "boss",    "title": "⚔️ BOSS"},
            {"id": "partner", "title": "🏗️ PARTNER"},
            {"id": "friend",  "title": "🌿 FRIEND"}
        ]
    )

async def send_step2_schedule(pid: str, to: str):
    await send_interactive_buttons(pid, to,
        body_text="Target locked. Now, choose your *Pulse* schedule. When should I serve your briefings?\n\n🌅 *Early:* 6AM, 10AM, 2PM, 6PM\n☀️ *Standard:* 8AM, 12PM, 4PM, 8PM\n🌙 *Late:* 10AM, 2PM, 6PM, 10PM",
        buttons=[
            {"id": "early",    "title": "🌅 Early"},
            {"id": "standard", "title": "☀️ Standard"},
            {"id": "late",     "title": "🌙 Late"}
        ]
    )

async def send_step2b_timezone(pid: str, to: str):
    await send_whatsapp_text(pid, to,
        "✅ Schedule locked.\n\nNow, what is your GMT/UTC Timezone Offset?\n\nType a number:\n• `5.5` for India\n• `-5` for EST\n• `0` for UK\n• `11` for Sydney"
    )

async def send_step3_mission(pid: str, to: str):
    await send_list_message(pid, to,
        body_text="Understood. Now, tell me the current mission.\n\n*What is your #1 priority right now?*",
        button_label="Select Mission",
        rows=[
            {"id": "fix",   "title": "🛡️ FIX",   "description": "My life is a mess. Clear the backlog and stop fires."},
            {"id": "grow",  "title": "📈 GROW",   "description": "Sales mode. More leads, cash, and deals."},
            {"id": "build", "title": "🛠️ BUILD",  "description": "Deep-work mode. Finish the MVP or start a project."},
            {"id": "rest",  "title": "⚖️ REST",   "description": "Burnt out. Focus on family and health."}
        ]
    )

async def send_step4_anchor1(pid: str, to: str, exclude: str = None):
    rows = [
        {"id": "clients",  "title": "💎 CLIENTS",  "description": "Revenue and business relationships."},
        {"id": "partners", "title": "🤝 PARTNERS", "description": "Collaborators and business allies."},
        {"id": "family",   "title": "👨‍👩‍👦 FAMILY",   "description": "Spouse, children, and loved ones."},
        {"id": "team",     "title": "🏢 TEAM",     "description": "Employees and direct reports."}
    ]
    if exclude:
        rows = [r for r in rows if r["id"] != exclude]

    await send_list_message(pid, to,
        body_text="Selection saved. Who is your *#1 Anchor?*\n\nThe person or group I should prioritize above all else.",
        button_label="Select Anchor",
        rows=rows
    )

async def send_step4_anchor2(pid: str, to: str, exclude: str):
    rows = [
        {"id": "clients",  "title": "💎 CLIENTS",  "description": "Revenue and business relationships."},
        {"id": "partners", "title": "🤝 PARTNERS", "description": "Collaborators and business allies."},
        {"id": "family",   "title": "👨‍👩‍👦 FAMILY",   "description": "Spouse, children, and loved ones."},
        {"id": "team",     "title": "🏢 TEAM",     "description": "Employees and direct reports."}
    ]
    rows = [r for r in rows if r["id"] != exclude]

    await send_list_message(pid, to,
        body_text="Good. Now who is your *#2 Anchor?*",
        button_label="Select Anchor",
        rows=rows
    )

# ─────────────────────────────────────────────
# Main Webhook Entry Point
# ─────────────────────────────────────────────

async def process_whatsapp_webhook(update: dict):
    """Parse Meta's nested webhook payload and route the message."""
    try:
        if update.get("object") != "whatsapp_business_account":
            return

        for entry in update.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})
                messages = value.get("messages", [])
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")

                if not phone_number_id:
                    continue

                for message in messages:
                    msg_type = message.get("type")
                    from_number = message.get("from")

                    # Extract text from either plain text or interactive reply
                    body = ""
                    interactive_id = ""

                    if msg_type == "text":
                        body = message.get("text", {}).get("body", "").strip()
                    elif msg_type == "interactive":
                        interactive = message.get("interactive", {})
                        itype = interactive.get("type")
                        if itype == "button_reply":
                            interactive_id = interactive.get("button_reply", {}).get("id", "")
                            body = interactive.get("button_reply", {}).get("title", "")
                        elif itype == "list_reply":
                            interactive_id = interactive.get("list_reply", {}).get("id", "")
                            body = interactive.get("list_reply", {}).get("title", "")
                    else:
                        continue  # Ignore images, audio, etc.

                    print(f"[WA] From: {from_number} | type: {msg_type} | id: {interactive_id} | body: {body}")

                    # Prefixed user_id for cross-platform isolation
                    user_id = f"wa_{from_number}"

                    await handle_message(
                        phone_number_id=phone_number_id,
                        from_number=from_number,
                        user_id=user_id,
                        body=body,
                        interactive_id=interactive_id,
                        value=value
                    )

    except Exception as e:
        print(f"[WA CRITICAL] {str(e)}")


async def handle_message(phone_number_id: str, from_number: str, user_id: str, body: str, interactive_id: str, value: dict):
    """5-step onboarding state machine + capture mode."""
    pid = phone_number_id
    supabase = await get_supabase()
    lower = body.lower().strip() if body else ""

    # ─── INITIALIZE / START ───
    if lower in ["initialize", "start", "hi", "hello", "/start"]:
        # Reset existing config
        await supabase.table('core_config').delete().eq('user_id', user_id).execute()
        # Save first name
        contacts = value.get("contacts", [])
        first_name = "Leader"
        if contacts:
            first_name = contacts[0].get("profile", {}).get("name", "Leader").split()[0]
        # Store join timestamp once — used for 14-day trial expiry
        from datetime import datetime, timezone
        await set_config(user_id, 'joined_at', datetime.now(timezone.utc).isoformat())
        await set_config(user_id, 'user_name', first_name)
        await send_step1_persona(pid, from_number)
        return

    # ─── FETCH STATE ───
    configs = await get_configs(user_id)
    identity   = next((c['content'] for c in configs if c['key'] == 'identity'), None)
    schedule   = next((c['content'] for c in configs if c['key'] == 'pulse_schedule'), None)
    tz_offset  = next((c['content'] for c in configs if c['key'] == 'timezone_offset'), None)
    mission    = next((c['content'] for c in configs if c['key'] == 'current_season'), None)
    anchor1    = next((c['content'] for c in configs if c['key'] == 'anchor_1'), None)
    anchor2    = next((c['content'] for c in configs if c['key'] == 'anchor_2'), None)
    setup_done = next((c['content'] for c in configs if c['key'] == 'initial_people_setup'), None)

    # ─── STEP 1: PERSONA ───
    if not identity:
        persona_map = {"boss": "1", "partner": "2", "friend": "3"}
        chosen = persona_map.get(interactive_id)
        if chosen:
            await set_config(user_id, 'identity', chosen)
            await send_step2_schedule(pid, from_number)
        else:
            await send_step1_persona(pid, from_number)
        return

    # ─── STEP 2a: SCHEDULE ───
    if not schedule:
        schedule_map = {"early": "1", "standard": "2", "late": "3"}
        chosen = schedule_map.get(interactive_id)
        if chosen:
            await set_config(user_id, 'pulse_schedule', chosen)
            await send_step2b_timezone(pid, from_number)
        else:
            await send_step2_schedule(pid, from_number)
        return

    # ─── STEP 2b: TIMEZONE (plain number text) ───
    if not tz_offset:
        import re
        match = re.search(r'-?\d+(\.\d+)?', body)
        if match:
            offset = match.group(0)
            await set_config(user_id, 'timezone_offset', offset)
            sign = "+" if float(offset) >= 0 else ""
            await send_whatsapp_text(pid, from_number, f"✅ Synced to GMT{sign}{offset}.")
            await send_step3_mission(pid, from_number)
        else:
            await send_whatsapp_text(pid, from_number, "⚠️ Please enter a valid number (e.g., `5.5`, `-5`, `0`).")
        return

    # ─── STEP 3: MISSION ───
    if not mission:
        mission_labels = {
            "fix":   "🛡️ FIX — Clear the backlog",
            "grow":  "📈 GROW — Sales mode",
            "build": "🛠️ BUILD — Deep work mode",
            "rest":  "⚖️ REST — Rest and recover"
        }
        label = mission_labels.get(interactive_id)
        if label:
            await set_config(user_id, 'current_season', label)
            await send_step4_anchor1(pid, from_number)
        else:
            await send_step3_mission(pid, from_number)
        return

    # ─── STEP 4a: ANCHOR 1 ───
    if not anchor1:
        anchor_ids = ["clients", "partners", "family", "team"]
        if interactive_id in anchor_ids:
            await set_config(user_id, 'anchor_1', interactive_id)
            await send_step4_anchor2(pid, from_number, exclude=interactive_id)
        else:
            await send_step4_anchor1(pid, from_number)
        return

    # ─── STEP 4b: ANCHOR 2 ───
    if not anchor2:
        anchor_ids = ["clients", "partners", "family", "team"]
        if interactive_id in anchor_ids and interactive_id != anchor1:
            await set_config(user_id, 'anchor_2', interactive_id)
            await set_config(user_id, 'initial_people_setup', 'true')

            # Send activation message
            anchor_display = {"clients": "💎 Clients", "partners": "🤝 Partners", "family": "👨‍👩‍👦 Family", "team": "🏢 Team"}
            user_name = next((c['content'] for c in configs if c['key'] == 'user_name'), 'Leader')
            activation_msg = (
                f"✅ *You are fully calibrated, {user_name}.*\n\n"
                f"🎯 *Mission:* {mission}\n"
                f"⚓ *Anchors:* {anchor_display.get(anchor1, anchor1)} → {anchor_display.get(interactive_id, interactive_id)}\n\n"
                "I will see you at your next scheduled Pulse.\n\n"
                "Until then, don't worry about formatting — just send me your raw thoughts, tasks, or updates here naturally. I'll handle the rest.\n\n"
                "_Reply 'ok' anytime to confirm receipt of your next briefing._"
            )
            await send_whatsapp_text(pid, from_number, activation_msg)
        else:
            await send_step4_anchor2(pid, from_number, exclude=anchor1)
        return

    # ─── CAPTURE MODE (Active User) ───
    if body and not setup_done:
        return  # Safety guard

    if body:
        await supabase.table('raw_dumps').insert([{'user_id': user_id, 'content': body, 'source': 'whatsapp'}]).execute()
        await send_whatsapp_text(pid, from_number, "✅")
