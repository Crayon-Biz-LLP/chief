"""
Chief OS — Intent Classification Engine

Classifies incoming user messages into actionable intents:
  TASK, NOTE, NOISE, QUERY, DELEGATE, CLARIFICATION_NEEDED

Multi-tenant: adapts persona and routing based on user configuration.
Uses the fastest Gemini model (flash-lite) for sub-2s classification.

Consumed by: webhook.py, whatsapp.py
"""

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_async_client, AsyncClient
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

CLASSIFICATION_MODEL = "gemini-2.0-flash-lite"

# Persona definitions map to user's chosen identity setting
PERSONA_MAP = {
    "1": {
        "name": "Commander",
        "style": "Direct, urgent, execution-focused. Short sentences. Action-first.",
    },
    "2": {
        "name": "Architect",
        "style": "Methodical, structured, systems-focused. Precise, organized.",
    },
    "3": {
        "name": "Nurturer",
        "style": "Balanced, empathetic, team-aware. Warm but professional.",
    },
}

DEFAULT_PERSONA = PERSONA_MAP["1"]

# ─────────────────────────────────────────────
# SINGLETONS
# ─────────────────────────────────────────────

_supabase_client: AsyncClient | None = None
_genai_client: genai.Client | None = None


async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY")
        )
    return _supabase_client


def get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _genai_client


# ─────────────────────────────────────────────
# CONTEXT HELPER
# ─────────────────────────────────────────────

async def get_recent_context(user_id: str, limit: int = 2) -> list[dict]:
    """Fetch the most recent unprocessed raw dumps for conversational context.

    This gives the classifier awareness of what the user said recently,
    enabling it to handle multi-message flows like:
      "Schedule a call with Sarah"  →  "Tomorrow at 3pm"
    """
    try:
        supabase = await get_supabase()
        result = await supabase.table("raw_dumps") \
            .select("content") \
            .eq("user_id", user_id) \
            .eq("is_processed", False) \
            .order("created_at", desc=True) \
            .limit(limit).execute()
        return result.data or []
    except Exception:
        return []


# ─────────────────────────────────────────────
# TIME PHASE HELPER
# ─────────────────────────────────────────────

def get_time_phase(hour: int) -> str:
    """Return a human-friendly time label for prompt context."""
    if 4 <= hour < 12:
        return "morning"
    elif 12 <= hour < 18:
        return "afternoon"
    else:
        return "night"


# ─────────────────────────────────────────────
# MAIN CLASSIFIER
# ─────────────────────────────────────────────

async def classify_intent(
    text: str,
    user_id: str,
    identity: str = "1",
    goal: str = "",
    tz_offset: float = 0.0,
) -> dict:
    """Classify a user message into an actionable intent.

    Args:
        text:       The raw user message
        user_id:    Tenant identifier
        identity:   User's persona choice ("1", "2", "3")
        goal:       User's current 14-day goal (season context)
        tz_offset:  GMT offset for time-aware responses

    Returns dict with keys:
        intent:     TASK | NOTE | NOISE | QUERY | DELEGATE | CLARIFICATION_NEEDED
        confidence: 0.0 – 1.0
        entity:     Suggested project routing (BIZ, PERSONAL, CHURCH, INBOX)
        title:      Extracted task/note title (literal from user's words)
        time_context: Any time/date info extracted
        receipt:    Short confirmation message for the user
        reasoning:  Brief classifier logic (for debugging)
    """
    # Resolve persona
    persona = PERSONA_MAP.get(identity, DEFAULT_PERSONA)

    # Get local time context
    local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    local_hour = local_now.hour
    time_phase = get_time_phase(local_hour)

    # Fetch recent messages for conversational context
    context = await get_recent_context(user_id, limit=2)
    context_str = ""
    if context:
        context_str = "\n\nPrevious messages:\n" + "\n".join(
            [f"- {c['content']}" for c in context]
        )

    prompt = f"""You are a message classifier for a task management system.
Your persona: {persona['name']} — {persona['style']}

Message: "{text}"{context_str}
Time of day: {time_phase}
User's main goal: {goal or "Not set"}

Return ONLY valid JSON (no markdown, no explanation):
{{
    "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY",
    "confidence": 0.0-1.0,
    "entity": "BIZ|PERSONAL|CHURCH|INBOX",
    "title": "extracted task title",
    "time_context": "time info if any",
    "receipt": "Short confirmation message.",
    "reasoning": "brief logic"
}}

Rules:
- TITLE FIDELITY: The title must be a literal extraction of the task as spoken. Never add project names or rephrase.
- TASK: Any message that implies an action (send, call, fix, check, review, etc). Does NOT require a date.
- NOTE: Ideas, insights, observations, or learnings worth remembering.
- QUERY: User is asking a question to retrieve information (e.g., "What did I say about...", "When is my meeting?").
- DELEGATE: Explicit research requests, competitor audits, "look into this".
- NOISE: Greetings, "ok", "thanks", casual conversation, acknowledgments.
- CLARIFICATION_NEEDED: Ambiguous input where the intent is genuinely unclear.
- ENTITY ROUTING: Route personal finances, bills, home, family to PERSONAL. Work/clients to BIZ. Faith/church to CHURCH. Unknown to INBOX.
- RECEIPT: Must be confirmation-only. Mirror the user's verb. Never mention entity names in the receipt.
- ZERO DATA LOSS: Never drop qualifiers (e.g., "Canadian project", "Zoho API").
- If time of day is night, after confirming the entry, optionally add a brief sign-off nudge.
- NEVER say "I'll ping", "I'll check", or "I'll handle it". You are a logging tool, not an agent."""

    try:
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=CLASSIFICATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        result = json.loads(response.text)

        # Validate required fields
        result.setdefault("intent", "CLARIFICATION_NEEDED")
        result.setdefault("confidence", 0.5)
        result.setdefault("entity", "INBOX")
        result.setdefault("title", text[:120])
        result.setdefault("time_context", "")
        result.setdefault("receipt", "Noted.")
        result.setdefault("reasoning", "")

        return result

    except Exception as e:
        print(f"[CLASSIFY ERROR] user={user_id}: {e}")
        return {
            "intent": "TASK",  # Fail-safe: treat as task to avoid data loss
            "confidence": 0.3,
            "entity": "INBOX",
            "title": text[:120],
            "time_context": "",
            "receipt": "Got it.",
            "reasoning": f"Classification error: {e}",
        }


# ─────────────────────────────────────────────
# STAGING AREA SORTER (Batch classification for Pulse)
# ─────────────────────────────────────────────

async def classify_dumps_batch(dumps: list[dict]) -> dict[str, list[int]]:
    """Batch-classify raw dumps into TASK / NOTE / NOISE categories.

    Used by Pulse before the main AI prompt to:
    1. Route NOTEs directly to the memories table
    2. Discard NOISE (mark as processed)
    3. Keep only TASKs for the main briefing prompt

    Args:
        dumps: List of dicts with 'id' and 'content' keys

    Returns:
        {"task_ids": [...], "note_ids": [...], "noise_ids": [...]}
    """
    if not dumps:
        return {"task_ids": [], "note_ids": [], "noise_ids": []}

    prompt = (
        "Categorize each input into one of three types:\n"
        "- TASK: Explicit action items, things to do, commitments, reminders\n"
        "- NOTE: Ideas, insights, observations, learnings, things worth remembering\n"
        "- NOISE: Casual conversation, acknowledgments, confirmations, low-value content\n\n"
        "Return ONLY a valid JSON array:\n"
        '[{"id": 1, "category": "TASK|NOTE|NOISE"}]\n\n'
        "Inputs:\n"
        + json.dumps(
            [{"id": d["id"], "content": d["content"][:400]} for d in dumps],
            indent=2,
        )
    )

    try:
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=CLASSIFICATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        classifications = json.loads(response.text)

        result: dict[str, list[int]] = {"task_ids": [], "note_ids": [], "noise_ids": []}

        for item in classifications:
            dump_id = item.get("id")
            category = (item.get("category") or "").upper()
            if category == "TASK":
                result["task_ids"].append(dump_id)
            elif category == "NOTE":
                result["note_ids"].append(dump_id)
            else:
                result["noise_ids"].append(dump_id)

        return result

    except Exception as e:
        print(f"[STAGING SORT ERROR] {e}")
        # Fail-safe: treat everything as a task to avoid data loss
        return {
            "task_ids": [d["id"] for d in dumps],
            "note_ids": [],
            "noise_ids": [],
        }


# ─────────────────────────────────────────────
# MULTIMODAL CONTENT EXTRACTION
# ─────────────────────────────────────────────

async def extract_multimodal_content(
    file_bytes: bytes,
    mime_type: str,
    user_id: str,
    identity: str = "1",
    goal: str = "",
    tz_offset: float = 0.0,
) -> list[dict]:
    """Process image / audio / document content through Gemini multimodal.

    Extracts structured items (tasks, notes) from visual, audio or document content.

    Returns a list of dicts: [{"type": "TASK"|"NOTE", "entity": "...", "content": "..."}]
    """
    persona = PERSONA_MAP.get(identity, DEFAULT_PERSONA)
    local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    time_phase = get_time_phase(local_now.hour)

    prompt = (
        f"You are a message classifier. Persona: {persona['name']}.\n"
        f"Time: {time_phase}. User's goal: {goal or 'Not set'}.\n\n"
        "INSTRUCTIONS:\n"
        "- If IMAGE: Transcribe text, analyze patterns, identify URLs or key data.\n"
        "- If AUDIO: Extract explicit actions, deadlines, decisions.\n"
        "- If DOCUMENT: Summarize intent, extract deliverables and deadlines.\n\n"
        "RULES:\n"
        "- TASK: Any implied action (send, call, fix, check).\n"
        "- NOTE: Strategic insights, facts, observations.\n"
        "- DELEGATE: Research requests or competitor audits.\n"
        "- Mirror the user's language. Do not add project names.\n"
        "- Never say 'I'll handle' — you are a logging tool.\n\n"
        "OUTPUT: Return ONLY a valid JSON array:\n"
        '[{"type": "TASK|NOTE|DELEGATE", "entity": "BIZ|PERSONAL|CHURCH|INBOX", "content": "..."}]'
    )

    try:
        content_parts = [prompt]
        # Gemini multimodal accepts inline bytes with mime type
        content_parts.append({"mime_type": mime_type, "data": file_bytes})

        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=CLASSIFICATION_MODEL,
            contents=content_parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

        return json.loads(response.text)

    except Exception as e:
        print(f"[MULTIMODAL ERROR] user={user_id}: {e}")
        return []
