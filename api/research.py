"""
Chief OS — Research Agent

Autonomous web research worker. Processes pending tasks from the agent_queue table.
Uses Jina Search for web retrieval and Gemini for synthesis into actionable dossiers.

Multi-tenant: each research task is scoped to a user_id.
Trigger: Called inline during pulse, or via a periodic GitHub Actions cron.
"""

import os
import json
import asyncio
from urllib.parse import quote

import httpx
from supabase import create_async_client, AsyncClient
from google import genai

from .pulse import send_message  # Re-use the unified notification router

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

SYNTHESIS_MODEL = "gemini-3.1-flash-lite-preview"
JINA_BASE_URL = "https://s.jina.ai"

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
# WEB SEARCH VIA JINA
# ─────────────────────────────────────────────

async def jina_search(query: str) -> str:
    """Search the web via Jina and return raw text results."""
    jina_key = os.getenv("JINA_API_KEY", "")
    headers = {
        "Accept": "application/json",
    }
    if jina_key:
        headers["Authorization"] = f"Bearer {jina_key}"

    encoded = quote(query)
    url = f"{JINA_BASE_URL}/{encoded}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            return response.text[:8000]  # Cap to prevent prompt bloat
    except Exception as e:
        print(f"[JINA ERROR] {e}")
        return f"Search failed: {e}"


# ─────────────────────────────────────────────
# SYNTHESIS
# ─────────────────────────────────────────────

async def synthesize_dossier(task_text: str, search_results: str) -> str:
    """Use Gemini to synthesize web search results into an actionable research dossier."""
    prompt = (
        f'The user delegated this research task: "{task_text}"\n\n'
        "Using the web search results below, create a concise, actionable research dossier.\n"
        "Focus on key findings, competitive insights, and recommended next steps.\n"
        "Format with clear headers and bullet points.\n\n"
        f"Web Search Results:\n{search_results}"
    )

    try:
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=SYNTHESIS_MODEL,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[SYNTHESIS ERROR] {e}")
        return f"Synthesis failed: {e}"


# ─────────────────────────────────────────────
# PROCESS PENDING RESEARCH FOR A USER
# ─────────────────────────────────────────────

async def process_user_research(user_id: str, max_tasks: int = 3):
    """Process pending research tasks for a specific user.

    Args:
        user_id:    Tenant identifier
        max_tasks:  Max tasks to process per call (prevents timeout on Vercel)
    """
    supabase = await get_supabase()

    try:
        res = await supabase.table("agent_queue") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "pending") \
            .limit(max_tasks) \
            .execute()

        pending = res.data or []
        if not pending:
            return

        print(f"[RESEARCH] user={user_id}: {len(pending)} pending task(s)")

        for item in pending:
            task_id = item.get("id")
            task_text = item.get("task", "")
            if not task_text:
                continue

            # Mark as processing
            await supabase.table("agent_queue") \
                .update({"status": "processing"}) \
                .eq("id", task_id).execute()

            try:
                # 1. Web search
                search_results = await jina_search(task_text)

                # 2. Synthesize into dossier
                dossier = await synthesize_dossier(task_text, search_results)

                # 3. Store dossier in raw_dumps for next pulse to process
                await supabase.table("raw_dumps").insert([{
                    "user_id": user_id,
                    "content": f"RESEARCH DOSSIER: {task_text}\n\n{dossier}",
                    "source": "research_agent",
                }]).execute()

                # 4. Notify user
                snippet = task_text[:40] + ("..." if len(task_text) > 40 else "")
                await send_message(user_id, f"🔍 *Research Complete:* {snippet}\n\nThe dossier will appear in your next briefing.")

                # 5. Mark completed
                await supabase.table("agent_queue").update({
                    "status": "completed",
                }).eq("id", task_id).execute()

                print(f"[RESEARCH] Completed: {task_text[:40]}...")

            except Exception as e:
                print(f"[RESEARCH ERROR] task={task_id}: {e}")
                await supabase.table("agent_queue").update({
                    "status": "failed",
                    "metadata": json.dumps({"error": str(e)}),
                }).eq("id", task_id).execute()

    except Exception as e:
        print(f"[RESEARCH CRITICAL] user={user_id}: {e}")


# ─────────────────────────────────────────────
# PROCESS ALL PENDING RESEARCH (for cron/batch)
# ─────────────────────────────────────────────

async def process_all_research():
    """Process pending research for all users. Called by GitHub Actions cron or API route."""
    supabase = await get_supabase()

    try:
        # Get distinct user_ids with pending research
        res = await supabase.table("agent_queue") \
            .select("user_id") \
            .eq("status", "pending") \
            .execute()

        if not res.data:
            print("[RESEARCH] No pending research tasks.")
            return

        user_ids = list(set(item["user_id"] for item in res.data))
        print(f"[RESEARCH] Found pending tasks for {len(user_ids)} user(s)")

        for uid in user_ids:
            await process_user_research(uid)

    except Exception as e:
        print(f"[RESEARCH MASTER ERROR] {e}")
