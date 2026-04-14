import os
import re
import json
import httpx
import hashlib
from datetime import datetime, timezone, timedelta
from supabase import create_async_client, AsyncClient
from .google_sync import has_google_connection
from .intent import classify_intent, extract_multimodal_content
from .memory import store_memory, interrogate_brain, extract_and_store_graph
from .billing import check_access, record_usage

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY")
        )
    return _supabase_client


# ─────────────────────────────────────────────
# TIMEZONE AUTO-DETECTION FROM PHONE NUMBER
# ─────────────────────────────────────────────
# WhatsApp numbers arrive as country_code + number (e.g. "919876543210").
# We match the leading digits to resolve GMT offset automatically.
# This eliminates an entire onboarding step.

COUNTRY_CODE_TO_TZ = {
    "91":  "5.5",   # India
    "977": "5.75",  # Nepal
    "92":  "5",     # Pakistan
    "94":  "5.5",   # Sri Lanka
    "880": "6",     # Bangladesh
    "95":  "6.5",   # Myanmar
    "66":  "7",     # Thailand
    "84":  "7",     # Vietnam
    "65":  "8",     # Singapore
    "60":  "8",     # Malaysia
    "63":  "8",     # Philippines
    "852": "8",     # Hong Kong
    "86":  "8",     # China
    "81":  "9",     # Japan
    "82":  "9",     # South Korea
    "971": "4",     # UAE
    "966": "3",     # Saudi Arabia
    "974": "3",     # Qatar
    "968": "4",     # Oman
    "973": "3",     # Bahrain
    "962": "3",     # Jordan
    "961": "2",     # Lebanon
    "972": "2",     # Israel
    "90":  "3",     # Turkey
    "254": "3",     # Kenya
    "255": "3",     # Tanzania
    "256": "3",     # Uganda
    "251": "3",     # Ethiopia
    "234": "1",     # Nigeria
    "233": "0",     # Ghana
    "27":  "2",     # South Africa
    "20":  "2",     # Egypt
    "212": "1",     # Morocco
    "44":  "0",     # UK
    "353": "0",     # Ireland
    "33":  "1",     # France
    "49":  "1",     # Germany
    "31":  "1",     # Netherlands
    "34":  "1",     # Spain
    "39":  "1",     # Italy
    "46":  "1",     # Sweden
    "47":  "1",     # Norway
    "41":  "1",     # Switzerland
    "48":  "1",     # Poland
    "380": "2",     # Ukraine
    "7":   "3",     # Russia (Moscow)
    "1":   "-5",    # US/Canada (Eastern default)
    "52":  "-6",    # Mexico
    "55":  "-3",    # Brazil
    "57":  "-5",    # Colombia
    "54":  "-3",    # Argentina
    "56":  "-4",    # Chile
    "51":  "-5",    # Peru
    "61":  "10",    # Australia (AEST)
    "64":  "12",    # New Zealand
}

def detect_timezone(phone_number: str) -> str:
    """Resolve GMT offset from WhatsApp phone number country code.
    Tries longest prefix first (3-digit codes like 971) before shorter ones."""
    for length in (3, 2, 1):
        prefix = phone_number[:length]
        if prefix in COUNTRY_CODE_TO_TZ:
            return COUNTRY_CODE_TO_TZ[prefix]
    return "0"  # Fallback to GMT


# ─────────────────────────────────────────────
# WHATSAPP CLOUD API — SEND HELPERS
# ─────────────────────────────────────────────

async def _wa_post(phone_number_id: str, payload: dict):
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if not response.is_success:
            print(f"[WA SEND ERROR] {response.status_code}: {response.text}")
        return response


async def send_text(pid: str, to: str, text: str, preview_url: bool = False):
    # Auto-prefix bot identity on all outgoing messages
    if not text.startswith("🤖"):
        text = f"🤖 {text}"
    await _wa_post(pid, {
        "messaging_product": "whatsapp", "to": to,
        "type": "text", "text": {"body": text, "preview_url": preview_url},
    })


async def send_buttons(pid: str, to: str, body: str, buttons: list):
    if not body.startswith("🤖"):
        body = f"🤖 {body}"
    await _wa_post(pid, {
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons[:3]
                ]
            },
        },
    })


async def send_list(pid: str, to: str, body: str, button_label: str, rows: list):
    if not body.startswith("🤖"):
        body = f"🤖 {body}"
    await _wa_post(pid, {
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list", "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": [{"title": "Options", "rows": rows}],
            },
        },
    })


async def mark_read(pid: str, message_id: str):
    """Mark incoming message as read (blue ticks)."""
    await _wa_post(pid, {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    })


async def download_whatsapp_media(media_id: str) -> tuple[bytes, str]:
    """Download media from WhatsApp Cloud API. Returns (file_bytes, mime_type)."""
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: Get media URL
            info_resp = await client.get(
                f"{WHATSAPP_API_URL}/{media_id}", headers=headers
            )
            info = info_resp.json()
            media_url = info.get("url", "")
            mime_type = info.get("mime_type", "application/octet-stream")
            if not media_url:
                raise Exception(f"No media URL in response: {info}")
            # Step 2: Download the file
            dl_resp = await client.get(media_url, headers=headers)
            return dl_resp.content, mime_type
    except Exception as e:
        print(f"[WA MEDIA ERROR] {e}")
        raise


# ─────────────────────────────────────────────
# SUPABASE CONFIG HELPERS
# ─────────────────────────────────────────────

async def set_config(user_id: str, key: str, content: str):
    supabase = await get_supabase()
    await supabase.table("core_config").delete().eq("user_id", user_id).eq("key", key).execute()
    await supabase.table("core_config").insert(
        [{"user_id": user_id, "key": key, "content": content}]
    ).execute()


async def del_config(user_id: str, key: str):
    supabase = await get_supabase()
    await supabase.table("core_config").delete().eq("user_id", user_id).eq("key", key).execute()


async def get_configs(user_id: str) -> list:
    supabase = await get_supabase()
    response = await supabase.table("core_config").select("key, content").eq("user_id", user_id).execute()
    return response.data or []


def cfg(configs: list, key: str):
    return next((c["content"] for c in configs if c["key"] == key), None)


# ─────────────────────────────────────────────
# TRIAL EXPIRY
# ─────────────────────────────────────────────

async def is_trial_expired(user_id: str) -> bool:
    supabase = await get_supabase()
    res = await supabase.table("core_config").select("content").eq("user_id", user_id).eq("key", "joined_at").limit(1).execute()
    if not res.data:
        return False
    try:
        joined = datetime.fromisoformat(res.data[0]["content"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - joined).total_seconds() > (14 * 86400)
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────
# ONBOARDING STEP SENDERS
# ─────────────────────────────────────────────
# Flow: Invite Code -> Step 1 (Mission Mode) -> Step 2 (Schedule) -> Step 3 (Goal) -> Active
# Timezone: AUTO-DETECTED from phone country code. Zero friction.

async def send_step1_mission(pid: str, to: str):
    await send_list(pid, to,
        body=(
            "⚡ *Step 1 of 3 — What's your mode?*\n\n"
            "This tells me what matters most to you right now.\n"
            "Pick one 👇"
        ),
        button_label="Pick Your Mode",
        rows=[
            {"id": "mission_fix",   "title": "🛡️ FIX",   "description": "My life's chaotic. Clear the backlog & fires."},
            {"id": "mission_grow",  "title": "📈 GROW",  "description": "Sales mode. More leads, cash & deals."},
            {"id": "mission_build", "title": "🛠️ BUILD", "description": "Deep work. Ship the product or project."},
            {"id": "mission_rest",  "title": "⚖️ REST",  "description": "Burnt out. Family, health & recovery."},
        ],
    )


async def send_step2_schedule(pid: str, to: str):
    await send_buttons(pid, to,
        body=(
            "🎯 Mode locked!\n\n"
            "*Step 2 of 3 — When should I check in?*\n\n"
            "🌅 Early → 6AM, 10AM, 2PM, 6PM\n"
            "☀️ Standard → 8AM, 12PM, 4PM, 8PM\n"
            "🌙 Late → 10AM, 2PM, 6PM, 10PM"
        ),
        buttons=[
            {"id": "sched_early",    "title": "🌅 Early"},
            {"id": "sched_standard", "title": "☀️ Standard"},
            {"id": "sched_late",     "title": "🌙 Late"},
        ],
    )


async def send_step3_goal(pid: str, to: str):
    await send_text(pid, to,
        "⏰ Schedule locked!\n\n"
        "*Step 3 of 3 — What's your #1 goal right now?*\n\n"
        "Tell me the ONE thing you're chasing over the next 14 days.\n\n"
        "Just type it naturally:\n"
        "• _Close the Series A term sheet_\n"
        "• _Ship MVP and get 10 beta users_\n"
        "• _Clear all debt and stabilize cash flow_\n"
        "• _Finish my thesis by March 30_"
    )


async def send_activation(pid: str, to: str, user_name: str, mission: str, schedule: str, tz: str, goal: str):
    mission_display = {
        "fix": "🛡️ FIX — Clear the chaos",
        "grow": "📈 GROW — Revenue mode",
        "build": "🛠️ BUILD — Deep work",
        "rest": "⚖️ REST — Recovery",
    }
    schedule_display = {
        "1": "🌅 Early (6AM, 10AM, 2PM, 6PM)",
        "2": "☀️ Standard (8AM, 12PM, 4PM, 8PM)",
        "3": "🌙 Late (10AM, 2PM, 6PM, 10PM)",
    }
    sign = "+" if float(tz) >= 0 else ""

    await send_text(pid, to,
        f"🚀 *You're live, {user_name}!*\n\n"
        f"🎯 Goal: {goal}\n"
        f"Mode: {mission_display.get(mission, mission)}\n"
        f"Pulse: {schedule_display.get(schedule, 'Standard')}\n"
        f"🌍 Timezone: GMT{sign}{tz}\n\n"
        "─────────────────\n"
        "*HOW IT WORKS*\n\n"
        "1️⃣ *Dump* — Send me any thought, task, or update. No formatting needed. Just talk.\n\n"
        "2️⃣ *I process* — At your Pulse times, my AI reads everything, "
        "extracts tasks, spots completions, and builds your briefing.\n\n"
        "3️⃣ *You receive* — A structured action plan lands automatically.\n\n"
        "─────────────────\n"
        "Type *menu* anytime for all commands.\n\n"
        "Or just start dumping your raw thoughts now 💭"
    )


# ─────────────────────────────────────────────
# COMMAND MENU & HANDLERS
# ─────────────────────────────────────────────

COMMAND_KEYWORDS = {
    "menu", "help", "commands",
    "urgent", "brief", "goal", "vault", "people",
    "settings", "reset", "mission", "library",
}


async def send_command_menu(pid: str, to: str):
    await send_list(pid, to,
        body="📋 *COMMAND CENTER*\nWhat do you need? Pick an action 👇",
        button_label="Open Menu",
        rows=[
            {"id": "cmd_urgent",   "title": "🔥 Urgent",   "description": "Your #1 fire right now."},
            {"id": "cmd_brief",    "title": "📊 Brief",    "description": "Top 5 active tasks."},
            {"id": "cmd_goal",     "title": "🎯 Goal",     "description": "Your current 14-day target."},
            {"id": "cmd_vault",    "title": "💭 Vault",    "description": "Last 5 memories & notes."},
            {"id": "cmd_mission",  "title": "🚀 Mission",  "description": "View or create strategic missions."},
            {"id": "cmd_library",  "title": "📚 Library",  "description": "Your saved links & resources."},
            {"id": "cmd_people",   "title": "👥 People",   "description": "Your registered stakeholders."},
            {"id": "cmd_settings", "title": "⚙️ Settings", "description": "Change schedule, goal, or mode."},
        ],
    )


async def handle_command(pid: str, to: str, user_id: str, command: str):
    supabase = await get_supabase()

    if command in ("urgent", "cmd_urgent"):
        res = await supabase.table("tasks").select("title") \
            .eq("user_id", user_id).eq("priority", "urgent").eq("status", "todo") \
            .limit(1).execute()
        if res.data:
            await send_text(pid, to, f"🔥 *ACTION REQUIRED:*\n\n{res.data[0]['title']}")
        else:
            await send_text(pid, to, "✅ No active fires. You're clear.")

    elif command in ("brief", "cmd_brief"):
        res = await supabase.table("tasks").select("title, priority") \
            .eq("user_id", user_id).eq("status", "todo") \
            .limit(10).execute()
        tasks = res.data or []
        if tasks:
            order = {"urgent": 0, "important": 1, "chores": 2, "ideas": 3}
            sorted_t = sorted(tasks, key=lambda t: order.get(t.get("priority", ""), 9))[:5]
            lines = []
            for t in sorted_t:
                icon = "[!]" if t["priority"] == "urgent" else "[-]" if t["priority"] == "important" else "[ ]"
                lines.append(f"{icon} {t['title']}")
            await send_text(pid, to, "📊 *EXECUTIVE BRIEF:*\n\n" + "\n".join(lines))
        else:
            await send_text(pid, to, "📭 Task list is empty. Send me some thoughts to get started!")

    elif command in ("goal", "cmd_goal"):
        configs = await get_configs(user_id)
        goal = cfg(configs, "current_season")
        mode = cfg(configs, "mission_mode")
        mode_labels = {"fix": "🛡️ FIX", "grow": "📈 GROW", "build": "🛠️ BUILD", "rest": "⚖️ REST"}
        text = f"🎯 *CURRENT GOAL:*\n\n{goal}" if goal else "No goal set."
        if mode:
            text += f"\n*Mode:* {mode_labels.get(mode, mode)}"
        await send_text(pid, to, text)

    elif command in ("vault", "cmd_vault"):
        # Show memories (notes + reflections) instead of just raw dumps
        mem_res = await supabase.table("memories").select("content, memory_type, created_at") \
            .eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
        mems = mem_res.data or []
        if mems:
            lines = []
            for m in mems:
                ts = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).strftime("%b %d")
                icon = "💡" if m["memory_type"] == "note" else "📝"
                preview = m["content"][:150] + ("..." if len(m["content"]) > 150 else "")
                lines.append(f"{icon} _{ts}:_ {preview}")
            await send_text(pid, to, "💭 *VAULT (Last 5):*\n\n" + "\n\n".join(lines))
        else:
            # Fallback to raw_dumps
            res = await supabase.table("raw_dumps").select("content, created_at") \
                .eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
            items = res.data or []
            if items:
                lines = []
                for i in items:
                    ts = datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")).strftime("%b %d, %I:%M%p")
                    preview = i["content"][:120] + ("..." if len(i["content"]) > 120 else "")
                    lines.append(f"_{ts}:_ {preview}")
                await send_text(pid, to, "💭 *VAULT (Last 5):*\n\n" + "\n\n".join(lines))
            else:
                await send_text(pid, to, "💭 Vault is empty. Send notes starting with N: to fill it!")

    elif command in ("mission", "cmd_mission"):
        m_res = await supabase.table("missions").select("title, status") \
            .eq("user_id", user_id).eq("status", "active").execute()
        missions = m_res.data or []
        if missions:
            m_lines = [f"• {m['title']}" for m in missions]
            await send_text(pid, to, "🚀 *ACTIVE MISSIONS:*\n\n" + "\n".join(m_lines))
        else:
            await send_text(pid, to, "🚀 No active missions. Type 'mission: [Goal]' to declare one.")

    elif command in ("library", "cmd_library"):
        lib_res = await supabase.table("resources").select("title, url, category") \
            .eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()
        items = lib_res.data or []
        if items:
            lines = [f"🔖 {i.get('title') or 'Untitled'}\n   {i.get('url', '')}" for i in items]
            await send_text(pid, to, "📚 *RESOURCE LIBRARY (Last 10):*\n\n" + "\n\n".join(lines))
        else:
            await send_text(pid, to, "📚 Library is empty. Send URLs and I'll save them.")

    elif command in ("people", "cmd_people"):
        res = await supabase.table("people").select("name, role").eq("user_id", user_id).execute()
        people = res.data or []
        if people:
            lines = [f"- {p['name']} ({p.get('role', 'Contact')})" for p in people]
            await send_text(pid, to, "👥 *STAKEHOLDERS:*\n\n" + "\n".join(lines))
        else:
            await send_text(pid, to, "👥 No stakeholders yet. They'll be auto-detected from your updates.")

    elif command in ("settings", "cmd_settings"):
        # Check if Google is already connected to show the right option
        google_connected = await has_google_connection(user_id)
        google_row = (
            {"id": "set_google", "title": "✅ Google Connected", "description": "Your calendar & tasks are syncing."}
            if google_connected else
            {"id": "set_google", "title": "📅 Connect Google", "description": "Sync tasks to Calendar & Google Tasks."}
        )
        await send_list(pid, to,
            body="⚙️ *SETTINGS*\nWhat would you like to change?",
            button_label="Open Settings",
            rows=[
                {"id": "set_schedule", "title": "🕐 Change Schedule",  "description": "Switch Early, Standard, or Late."},
                {"id": "set_mode",     "title": "🎯 Change Mode",      "description": "Switch FIX, GROW, BUILD, or REST."},
                {"id": "set_goal",     "title": "📝 Change Goal",      "description": "Redefine your 14-day objective."},
                {"id": "set_timezone", "title": "🌍 Fix Timezone",     "description": "Override auto-detected timezone."},
                google_row,
                {"id": "set_reset",    "title": "🔄 Full Reset",       "description": "Wipe config and start fresh."},
            ],
        )

    elif command in ("reset", "set_reset"):
        await send_buttons(pid, to,
            body="⚠️ *Are you sure?*\n\nThis wipes your configuration and restarts setup.\nYour captured data (tasks, vault) stays safe.",
            buttons=[
                {"id": "confirm_reset", "title": "Yes, Reset"},
                {"id": "cancel_reset",  "title": "Cancel"},
            ],
        )


# ─────────────────────────────────────────────
# SETTINGS CHANGE HANDLERS
# ─────────────────────────────────────────────

async def handle_settings_action(pid: str, to: str, user_id: str, action_id: str):

    if action_id == "set_schedule":
        await set_config(user_id, "_pending_change", "schedule")
        await send_buttons(pid, to,
            body="🕐 *Pick your new schedule:*",
            buttons=[
                {"id": "sched_early",    "title": "🌅 Early"},
                {"id": "sched_standard", "title": "☀️ Standard"},
                {"id": "sched_late",     "title": "🌙 Late"},
            ],
        )

    elif action_id == "set_mode":
        await set_config(user_id, "_pending_change", "mode")
        await send_list(pid, to,
            body="🎯 *Pick your new mode:*",
            button_label="Select Mode",
            rows=[
                {"id": "mission_fix",   "title": "🛡️ FIX",   "description": "Firefighting. Clear debt, backlog, chaos."},
                {"id": "mission_grow",  "title": "📈 GROW",  "description": "Sales mode. Revenue and deals."},
                {"id": "mission_build", "title": "🛠️ BUILD", "description": "Deep work. Ship the product."},
                {"id": "mission_rest",  "title": "⚖️ REST",  "description": "Recovery. Family and health."},
            ],
        )

    elif action_id == "set_goal":
        await set_config(user_id, "_pending_change", "goal")
        await send_text(pid, to, "✏️ Type your new 14-day goal below:")

    elif action_id == "set_timezone":
        await set_config(user_id, "_pending_change", "timezone")
        await send_text(pid, to,
            "🌍 *Override Timezone*\n\n"
            "Type your GMT offset as a number:\n"
            "• 5.5 → India\n"
            "• -5 → US Eastern\n"
            "• 0 → UK\n"
            "• 8 → Singapore\n"
            "• 3 → Dubai/Nairobi"
        )

    elif action_id == "set_google":
        google_connected = await has_google_connection(user_id)
        if google_connected:
            await send_text(pid, to,
                "✅ *Google is already connected!*\n\n"
                "Your tasks and calendar events are syncing automatically."
            )
        else:
            short_url = f"https://chief-three.vercel.app/api/auth/google?user={user_id}"
            await send_text(pid, to,
                "📅 *Connect Google Calendar & Tasks*\n\n"
                "Tap the link below to grant access. "
                "Chief will sync your tasks to Google Calendar and Google Tasks automatically.\n\n"
                f"👉 {short_url}\n\n"
                "_You'll be redirected back here once done._"
            )


# ─────────────────────────────────────────────
# MAIN WEBHOOK ENTRY POINT
# ─────────────────────────────────────────────

async def process_whatsapp_webhook(update: dict):
    """Parse Meta's nested webhook payload and route each message."""
    try:
        if update.get("object") != "whatsapp_business_account":
            return

        for entry in update.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")
                if not phone_number_id:
                    continue

                # Ignore status-only updates (delivered, read receipts)
                if value.get("statuses") and not value.get("messages"):
                    continue

                for message in value.get("messages", []):
                    msg_type = message.get("type")
                    from_number = message.get("from")
                    msg_id = message.get("id")

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
                    elif msg_type in ("image", "audio", "voice", "document"):
                        # Caption text (images/documents can have captions)
                        body = message.get(msg_type, {}).get("caption", "").strip()
                    else:
                        await send_text(phone_number_id, from_number,
                            "📝 I only process text and media right now.")
                        continue

                    # Blue ticks
                    if msg_id:
                        await mark_read(phone_number_id, msg_id)

                    user_id = f"wa_{from_number}"
                    print(f"[WA] From: {from_number} | type: {msg_type} | iid: {interactive_id} | body: {body[:80]}")

                    await handle_message(
                        pid=phone_number_id,
                        to=from_number,
                        user_id=user_id,
                        body=body,
                        interactive_id=interactive_id,
                        value=value,
                        msg_type=msg_type,
                        message=message,
                    )

    except Exception as e:
        print(f"[WA CRITICAL] {str(e)}")


# ─────────────────────────────────────────────
# CORE MESSAGE HANDLER (STATE MACHINE)
# ─────────────────────────────────────────────
# 3 Phases:
#   1. GATEKEEPER -- Invite code
#   2. ONBOARDING -- Mission Mode -> Schedule -> Goal (tz auto)
#   3. ACTIVE -- Capture + Commands + Settings

async def handle_message(pid: str, to: str, user_id: str, body: str, interactive_id: str, value: dict, msg_type: str = "text", message: dict | None = None):
    supabase = await get_supabase()
    lower = body.lower().strip() if body else ""

    # Fetch all state in one query
    configs = await get_configs(user_id)
    invite_status = cfg(configs, "invite_status")
    mission_mode  = cfg(configs, "mission_mode")
    schedule      = cfg(configs, "pulse_schedule")
    goal          = cfg(configs, "current_season")
    setup_done    = cfg(configs, "initial_people_setup")
    pending       = cfg(configs, "_pending_change")

    # ===========================================================
    # PHASE 1: GATEKEEPER (Invite Code)
    # ===========================================================
    if not invite_status:
        INVITE_CODE = os.getenv("INVITE_CODE", "chief2026").lower()

        if lower == INVITE_CODE:
            # Wipe any stale config from prior sessions before fresh setup
            await supabase.table("core_config").delete().eq("user_id", user_id).execute()

            await set_config(user_id, "invite_status", "approved")

            contacts = value.get("contacts", [])
            first_name = (
                contacts[0].get("profile", {}).get("name", "there").split()[0]
                if contacts else "there"
            )
            await set_config(user_id, "joined_at", datetime.now(timezone.utc).isoformat())
            await set_config(user_id, "user_name", first_name)

            # Auto-detect timezone from phone number
            tz_offset = detect_timezone(to)
            await set_config(user_id, "timezone_offset", tz_offset)

            sign = "+" if float(tz_offset) >= 0 else ""
            await send_text(pid, to,
                f"👋 *Welcome, {first_name}!* You're in.\n\n"
                f"🌍 Timezone auto-detected: GMT{sign}{tz_offset}\n\n"
                "Let's get you set up in 60 seconds."
            )
            await send_step1_mission(pid, to)
        else:
            await send_text(pid, to,
                "🔒 *Access Restricted*\n\n"
                "This is *Chief* — your AI-powered Chief of Staff on WhatsApp.\n\n"
                "Send your invite code to get started."
            )
        return

    # ===========================================================
    # GLOBAL ACTIONS (work in any state)
    # ===========================================================

    if interactive_id == "confirm_reset":
        await supabase.table("core_config").delete().eq("user_id", user_id).execute()
        await set_config(user_id, "invite_status", "approved")
        contacts = value.get("contacts", [])
        first_name = contacts[0].get("profile", {}).get("name", "there").split()[0] if contacts else "there"
        await set_config(user_id, "joined_at", datetime.now(timezone.utc).isoformat())
        await set_config(user_id, "user_name", first_name)
        tz_offset = detect_timezone(to)
        await set_config(user_id, "timezone_offset", tz_offset)
        await send_text(pid, to, "*🔄 Reset complete.* Let's set you up again.")
        await send_step1_mission(pid, to)
        return

    if interactive_id == "cancel_reset":
        await send_text(pid, to, "Reset cancelled. Still operational. 👍")
        return

    if lower in ("start", "initialize", "/start", "restart"):
        await handle_command(pid, to, user_id, "reset")
        return

    # ===========================================================
    # PHASE 2: ONBOARDING (3 steps, tz auto-detected)
    # ===========================================================

    # Step 1: Mission Mode
    if not mission_mode:
        mission_map = {
            "mission_fix": "fix", "mission_grow": "grow",
            "mission_build": "build", "mission_rest": "rest",
        }
        chosen = mission_map.get(interactive_id)
        if chosen:
            await set_config(user_id, "mission_mode", chosen)
            await send_step2_schedule(pid, to)
        else:
            await send_step1_mission(pid, to)
        return

    # Step 2: Schedule
    if not schedule:
        schedule_map = {"sched_early": "1", "sched_standard": "2", "sched_late": "3"}
        chosen = schedule_map.get(interactive_id)
        if chosen:
            await set_config(user_id, "pulse_schedule", chosen)
            await send_step3_goal(pid, to)
        else:
            await send_step2_schedule(pid, to)
        return

    # Step 3: Goal
    if not goal:
        if body and len(body) >= 5 and not body.startswith("/"):
            await set_config(user_id, "current_season", body)
            await set_config(user_id, "initial_people_setup", "true")

            user_name = cfg(configs, "user_name") or "there"
            tz = cfg(configs, "timezone_offset") or detect_timezone(to)
            await send_activation(pid, to, user_name, mission_mode, schedule, tz, body)

            # Offer Google Calendar connect after activation
            short_url = f"https://chief-three.vercel.app/api/auth/google?user={user_id}"
            await send_text(pid, to,
                "📅 *One more thing — connect Google?*\n\n"
                "Chief can auto-sync your tasks to Google Calendar and Google Tasks.\n\n"
                f"👉 {short_url}\n\n"
                "_Optional — you can always do this later from *settings*._"
            )
        else:
            await send_text(pid, to, "✏️ Please type your main goal for the next 14 days (at least a few words).")
        return

    # ===========================================================
    # PHASE 3: ACTIVE USER
    # ===========================================================

    if not setup_done:
        await set_config(user_id, "initial_people_setup", "true")

    # ── Access Control (replaces hardcoded 14-day trial) ──
    access = await check_access(user_id)
    if not access["allowed"]:
        reason = access.get("reason", "Your plan has expired. Contact your admin.")
        await send_text(pid, to, f"⏳ {reason}")
        return

    # Record inbound usage
    await record_usage(user_id, "message_in", channel="whatsapp")

    # Handle Pending Settings Changes
    if pending:
        if pending == "schedule":
            schedule_map = {"sched_early": "1", "sched_standard": "2", "sched_late": "3"}
            chosen = schedule_map.get(interactive_id)
            if chosen:
                await set_config(user_id, "pulse_schedule", chosen)
                await del_config(user_id, "_pending_change")
                names = {"1": "🌅 Early", "2": "☀️ Standard", "3": "🌙 Late"}
                await send_text(pid, to, f"✅ *Schedule updated to {names[chosen]}.*")
            else:
                await send_buttons(pid, to, body="Pick your new schedule:",
                    buttons=[
                        {"id": "sched_early", "title": "🌅 Early"},
                        {"id": "sched_standard", "title": "☀️ Standard"},
                        {"id": "sched_late", "title": "🌙 Late"},
                    ])
            return

        elif pending == "mode":
            mission_map = {
                "mission_fix": "fix", "mission_grow": "grow",
                "mission_build": "build", "mission_rest": "rest",
            }
            chosen = mission_map.get(interactive_id)
            if chosen:
                await set_config(user_id, "mission_mode", chosen)
                await del_config(user_id, "_pending_change")
                labels = {"fix": "🛡️ FIX", "grow": "📈 GROW", "build": "🛠️ BUILD", "rest": "⚖️ REST"}
                await send_text(pid, to, f"✅ *Mode updated to {labels[chosen]}.*")
            else:
                await handle_settings_action(pid, to, user_id, "set_mode")
            return

        elif pending == "goal":
            if body and len(body) >= 5:
                await set_config(user_id, "current_season", body)
                await del_config(user_id, "_pending_change")
                await send_text(pid, to, f"✅ *Goal updated:* {body}")
            else:
                await send_text(pid, to, "✏️ Please type your new goal (at least a few words).")
            return

        elif pending == "timezone":
            match = re.search(r"-?\d+(\.\d+)?", body)
            if match:
                offset = match.group(0)
                await set_config(user_id, "timezone_offset", offset)
                await del_config(user_id, "_pending_change")
                sign = "+" if float(offset) >= 0 else ""
                await send_text(pid, to, f"✅ *Timezone updated to GMT{sign}{offset}.*")
            else:
                await send_text(pid, to, "🔢 Please type a number (e.g., 5.5, -5, 0).")
            return

    # Handle Settings List Replies
    if interactive_id in ("set_schedule", "set_mode", "set_goal", "set_timezone", "set_google", "set_reset"):
        if interactive_id == "set_reset":
            await handle_command(pid, to, user_id, "reset")
        else:
            await handle_settings_action(pid, to, user_id, interactive_id)
        return

    # Handle Command Menu Replies
    if interactive_id and interactive_id.startswith("cmd_"):
        await handle_command(pid, to, user_id, interactive_id)
        return

    # Handle Text Commands
    if lower in COMMAND_KEYWORDS:
        if lower in ("menu", "help", "commands"):
            await send_command_menu(pid, to)
        else:
            await handle_command(pid, to, user_id, lower)
        return

    # ===========================================================
    # BRAIN INTERROGATION — ?query syntax
    # ===========================================================
    if lower.startswith("?"):
        query = body[1:].strip()
        if query:
            await send_text(pid, to, "🧠 _Searching your vault..._")
            answer = await interrogate_brain(user_id, query)
            await send_text(pid, to, f"🧠 {answer}")
            await record_usage(user_id, "brain_query", channel="whatsapp")
        else:
            await send_text(pid, to, "Send a question after ? to search your vault.")
        return

    # ===========================================================
    # EXPLICIT NOTE CAPTURE — N: or Note: prefix
    # ===========================================================
    if lower.startswith("n:") or lower.startswith("note:"):
        note_content = body.split(":", 1)[1].strip()
        if note_content:
            mem_id = await store_memory(user_id, note_content, memory_type="note")
            if mem_id:
                try:
                    await extract_and_store_graph(user_id, note_content, memory_id=mem_id)
                except Exception:
                    pass
            await send_text(pid, to, "📝 Note vaulted.")
        else:
            await send_text(pid, to, "Type your note after N: — e.g., N: The client prefers a phased rollout")
        return

    # ===========================================================
    # MISSION CREATION — mission: [goal] syntax
    # ===========================================================
    if lower.startswith("mission:") or lower.startswith("/mission "):
        mission_text = body.split(":", 1)[1].strip() if ":" in body else body.replace("/mission ", "").strip()
        if mission_text and len(mission_text) >= 5:
            try:
                await supabase.table("missions").insert({
                    "user_id": user_id, "title": mission_text, "status": "active"
                }).execute()
                await send_text(pid, to, f"🚀 *Mission declared:* {mission_text}")
            except Exception:
                await send_text(pid, to, "❌ Error creating mission.")
        else:
            await send_text(pid, to, "Type 'mission: [Your strategic goal]' to declare one.")
        return

    # ===========================================================
    # MULTIMODAL INPUT — Images, Audio, Documents
    # ===========================================================
    if not body and msg_type in ("image", "audio", "voice", "document"):
        msg = message or {}
        media_id = None
        media_label = ""

        if msg_type == "image":
            media_id = msg.get("image", {}).get("id")
            media_label = "image"
        elif msg_type in ("audio", "voice"):
            media_id = msg.get(msg_type, {}).get("id")
            media_label = "audio"
        elif msg_type == "document":
            media_id = msg.get("document", {}).get("id")
            doc_mime = msg.get("document", {}).get("mime_type", "")
            if doc_mime in ("application/pdf",) or doc_mime.startswith("text/"):
                media_label = "document"
                media_label = "document"
            else:
                await send_text(pid, to, "⚠️ Unsupported file type. Send as PDF, image, or voice.")
                return

        if media_id and media_label:
            await send_text(pid, to, f"📎 Processing {media_label}...")
            try:
                file_bytes, mime = await download_whatsapp_media(media_id)

                identity_val = cfg(configs, "identity") or "1"
                goal_val = cfg(configs, "current_season") or ""
                tz_val = float(cfg(configs, "timezone_offset") or "0")

                extracted = await extract_multimodal_content(
                    file_bytes, mime, user_id,
                    identity=identity_val, goal=goal_val, tz_offset=tz_val,
                )

                task_count = 0
                note_count = 0
                for item in extracted:
                    item_type = (item.get("type") or "").upper()
                    content = item.get("content", "")
                    if not content:
                        continue
                    if item_type == "TASK":
                        await supabase.table("raw_dumps").insert(
                            [{"user_id": user_id, "content": content, "source": "wa_media"}]
                        ).execute()
                        task_count += 1
                    elif item_type == "NOTE":
                        await store_memory(user_id, content, memory_type="note")
                        note_count += 1
                    elif item_type == "DELEGATE":
                        await supabase.table("agent_queue").insert({
                            "user_id": user_id, "task": content, "status": "pending"
                        }).execute()

                parts = []
                if task_count:
                    parts.append(f"{task_count} task{'s' if task_count != 1 else ''}")
                if note_count:
                    parts.append(f"{note_count} note{'s' if note_count != 1 else ''}")
                summary = " & ".join(parts) if parts else "Content"
                await send_text(pid, to, f"✅ Logged {summary}.")
                await record_usage(user_id, "media_process", channel="whatsapp")
            except Exception as e:
                print(f"[WA MULTIMODAL ERROR] user={user_id}: {e}")
                await send_text(pid, to, "⚠️ Couldn't process that file. Try sending as text.")
            return

    # ===========================================================
    # SMART CAPTURE — Intent Classification (Default)
    # ===========================================================
    if body:
        identity_val = cfg(configs, "identity") or "1"
        goal_val = cfg(configs, "current_season") or ""
        tz_val = float(cfg(configs, "timezone_offset") or "0")

        classification = await classify_intent(
            text=body,
            user_id=user_id,
            identity=identity_val,
            goal=goal_val,
            tz_offset=tz_val,
        )

        intent = classification.get("intent", "TASK")
        confidence = classification.get("confidence", 0.5)
        receipt = classification.get("receipt", "Got it.")

        print(f"[WA CLASSIFY] user={user_id} intent={intent} ({confidence:.0%}) — {body[:60]}...")

        if intent == "TASK" and confidence >= 0.5:
            await supabase.table("raw_dumps").insert(
                [{"user_id": user_id, "content": body, "source": "whatsapp"}]
            ).execute()
            await send_text(pid, to, f"✅ {receipt}")

        elif intent == "QUERY" and confidence >= 0.5:
            await send_text(pid, to, "� _Searching your vault..._")
            answer = await interrogate_brain(user_id, body)
            await send_text(pid, to, f"🧠 {answer}")

        elif intent == "NOTE" and confidence >= 0.5:
            if re.match(r"https?://", body.strip()):
                await supabase.table("resources").insert({
                    "user_id": user_id,
                    "url": body.strip(),
                    "title": classification.get("title", "New Resource"),
                    "category": "LINK",
                }).execute()
                await send_text(pid, to, "🔖 Resource saved to Library.")
            else:
                mem_id = await store_memory(user_id, body, memory_type="note")
                if mem_id:
                    try:
                        await extract_and_store_graph(user_id, body, memory_id=mem_id)
                    except Exception:
                        pass
                await send_text(pid, to, f"📝 {receipt}")

        elif intent == "DELEGATE":
            await supabase.table("agent_queue").insert({
                "user_id": user_id, "task": body, "status": "pending"
            }).execute()
            await send_text(pid, to, f"🔍 {receipt}")

        elif intent == "NOISE":
            await supabase.table("raw_dumps").insert(
                [{"user_id": user_id, "content": body, "source": "whatsapp"}]
            ).execute()
            await send_text(pid, to, "👍")

        elif intent == "CLARIFICATION_NEEDED":
            await supabase.table("raw_dumps").insert(
                [{"user_id": user_id, "content": body, "source": "whatsapp"}]
            ).execute()
            question = classification.get("reasoning", "Could you provide more details?")
            await send_text(pid, to, f"🤔 {receipt}\n\n{question}")

        else:
            # Fail-safe: treat as task to avoid data loss
            await supabase.table("raw_dumps").insert(
                [{"user_id": user_id, "content": body, "source": "whatsapp"}]
            ).execute()
            await send_text(pid, to, f"✅ {receipt}")
