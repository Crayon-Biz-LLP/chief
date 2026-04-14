"""
Chief OS — Memory & Knowledge Graph Layer

Multi-tenant semantic memory (embeddings + vector search) and knowledge graph
(nodes + edges + hybrid search). All functions are async, tenant-isolated, and
safe for concurrent use across users.

Consumed by: webhook.py (note capture, brain interrogation)
              pulse.py   (hindsight retrieval, graph context, enrichment)
              research.py (dossier storage)
"""

import os
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_async_client, AsyncClient
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768
LITE_MODEL = "gemini-2.0-flash-lite"

# ─────────────────────────────────────────────
# SINGLETONS (cold-start safe for Vercel serverless)
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


# ═══════════════════════════════════════════════
# SECTION 1: EMBEDDINGS
# ═══════════════════════════════════════════════

def get_embedding(text: str) -> list[float]:
    """Generate a 768-dim embedding for text using Gemini.

    Returns a zero-vector on failure rather than crashing — allows the
    calling pipeline to degrade gracefully (store without vector, skip
    from similarity search).
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIMENSION
    try:
        client = get_genai_client()
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text[:2000],          # Truncate to keep cost/latency low
            config={"output_dimensionality": EMBEDDING_DIMENSION},
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"[EMBEDDING ERROR] {e}")
        return [0.0] * EMBEDDING_DIMENSION


async def get_embedding_async(text: str) -> list[float]:
    """Non-blocking wrapper — runs the sync Gemini call in a thread pool."""
    return await asyncio.to_thread(get_embedding, text)


# ═══════════════════════════════════════════════
# SECTION 2: MEMORY STORAGE & RETRIEVAL
# ═══════════════════════════════════════════════

async def store_memory(
    user_id: str,
    content: str,
    memory_type: str = "note",
    metadata: dict | None = None,
) -> int | None:
    """Store content as a searchable memory with embedding.

    Args:
        user_id:     Tenant identifier (e.g. "wa_919876543210" or TG chat_id)
        content:     The text to remember
        memory_type: note | reflection | insight | observation
        metadata:    Optional JSON metadata dict

    Returns:
        The memory row id, or None on failure.
    """
    if not content or not content.strip():
        return None
    try:
        embedding = await get_embedding_async(content)
        supabase = await get_supabase()
        result = await supabase.table("memories").insert({
            "user_id": user_id,
            "content": content,
            "memory_type": memory_type,
            "metadata": metadata or {},
            "embedding": embedding,
        }).execute()
        row = result.data[0] if result.data else None
        return row["id"] if row else None
    except Exception as e:
        print(f"[MEMORY STORE ERROR] user={user_id}: {e}")
        return None


async def retrieve_memories(
    user_id: str,
    query: str,
    top_k: int = 5,
    threshold: float = 0.55,
) -> list[dict]:
    """Semantic search across a user's memories.

    Returns a list of dicts: {id, content, memory_type, metadata, similarity, created_at}
    sorted by descending similarity.
    """
    if not query or not query.strip():
        return []
    try:
        embedding = await get_embedding_async(query)
        if not any(embedding):
            return []
        supabase = await get_supabase()
        result = await supabase.rpc(
            "match_memories",
            {
                "query_embedding": embedding,
                "match_count": top_k,
                "match_threshold": threshold,
                "filter_user_id": user_id,
            },
        ).execute()
        return result.data or []
    except Exception as e:
        print(f"[MEMORY SEARCH ERROR] user={user_id}: {e}")
        return []


async def retrieve_hindsight(
    user_id: str,
    task_inputs: list[str],
    active_tasks: list[dict],
    top_k: int = 5,
) -> tuple[list[str], bool]:
    """Multi-signal vector recall for the Pulse briefing context.

    Searches against:
      1. Combined text of new raw dumps
      2. Top 3 urgent active task titles

    Returns:
        (formatted_memory_lines, is_stale)
        is_stale = True if the most recent memory is >36 hours old
    """
    search_queries: list[str] = []

    if task_inputs:
        combined = " ".join(task_inputs)[:500]
        search_queries.append(combined)

    # Add titles of top urgent tasks as search signals
    urgent_first = sorted(
        active_tasks,
        key=lambda t: t.get("priority", "") == "urgent",
        reverse=True,
    )
    for t in urgent_first[:3]:
        title = t.get("title", "")
        if title:
            search_queries.append(title)

    if not search_queries:
        return ([], False)

    # Fire all searches in parallel
    tasks_coros = [retrieve_memories(user_id, q, top_k=top_k) for q in search_queries]
    all_results = await asyncio.gather(*tasks_coros, return_exceptions=True)

    # De-duplicate by memory id
    seen_ids: set[int] = set()
    unique: list[dict] = []
    for result in all_results:
        if isinstance(result, Exception):
            continue
        for m in result:
            m_id = m.get("id")
            if m_id and m_id not in seen_ids:
                seen_ids.add(m_id)
                unique.append(m)

    unique.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    top = unique[:top_k]

    if not top:
        return ([], False)

    # Staleness check
    is_stale = False
    latest_ts = top[0].get("created_at")
    if latest_ts:
        try:
            last_seen = datetime.fromisoformat(str(latest_ts).replace("Z", "+00:00"))
            is_stale = (datetime.now(timezone.utc) - last_seen).total_seconds() > (36 * 3600)
        except (ValueError, TypeError):
            pass

    formatted = [
        f"[{m.get('memory_type', 'memory').upper()}] {m.get('content', '')}"
        for m in top
    ]
    return (formatted, is_stale)


async def generate_after_action_report(user_id: str, local_date: datetime) -> str | None:
    """Generate a brief end-of-day reflection and save to memories.

    Called by pulse.py when hour >= 20.
    """
    try:
        supabase = await get_supabase()
        today_start = local_date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        done_res = await supabase.table("tasks").select("title") \
            .eq("user_id", user_id).eq("status", "done") \
            .gte("completed_at", today_start).execute()
        done_count = len(done_res.data) if done_res.data else 0

        open_res = await supabase.table("tasks").select("id") \
            .eq("user_id", user_id).eq("status", "todo").execute()
        open_count = len(open_res.data) if open_res.data else 0

        client = get_genai_client()
        prompt = (
            f"Write a dry, 1-2 sentence after-action summary. "
            f"Loops closed today: {done_count}. Loops still open: {open_count}. "
            f"Be concise and factual. No motivational fluff."
        )
        response = await client.aio.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
        )
        lesson = response.text.strip()

        if lesson and len(lesson) > 10:
            await store_memory(user_id, lesson, memory_type="reflection")
            print(f"[AAR] user={user_id}: {lesson[:60]}...")
            return lesson
    except Exception as e:
        print(f"[AAR ERROR] user={user_id}: {e}")
    return None


# ═══════════════════════════════════════════════
# SECTION 3: KNOWLEDGE GRAPH — NODES & EDGES
# ═══════════════════════════════════════════════

async def ensure_graph_node(
    user_id: str,
    label: str,
    node_type: str = "concept",
    metadata: dict | None = None,
) -> str | None:
    """Get or create a graph node. Returns the node UUID.

    Uses upsert on (user_id, label) unique index to prevent duplicates.
    """
    if not label or not label.strip():
        return None
    try:
        supabase = await get_supabase()
        result = await supabase.table("graph_nodes").upsert(
            {
                "user_id": user_id,
                "label": label.strip(),
                "type": node_type,
                "metadata": metadata or {},
            },
            on_conflict="user_id,label",
        ).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        print(f"[GRAPH NODE ERROR] user={user_id}, label={label}: {e}")
        return None


async def create_graph_edge(
    user_id: str,
    source_node_id: str,
    target_node_id: str,
    relationship: str,
    metadata: dict | None = None,
) -> bool:
    """Create a directed edge between two graph nodes."""
    if not source_node_id or not target_node_id:
        return False
    try:
        supabase = await get_supabase()
        await supabase.table("graph_edges").insert({
            "user_id": user_id,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "relationship": relationship,
            "metadata": metadata or {},
        }).execute()
        return True
    except Exception as e:
        print(f"[GRAPH EDGE ERROR] user={user_id}: {e}")
        return False


async def extract_and_store_graph(user_id: str, text: str, memory_id: int | None = None):
    """Use Gemini to extract entities and relationships, then store in the graph.

    This is called after storing a note/memory to automatically build the
    user's knowledge graph from their content.
    """
    if not text or len(text) < 20:
        return

    try:
        client = get_genai_client()
        prompt = (
            "Extract knowledge graph elements from this text.\n\n"
            "Return a JSON object with:\n"
            '- "nodes": array of {"label": string, "type": "person"|"organization"|"project"|"concept"}\n'
            '- "edges": array of {"source": string, "target": string, "relationship": string}\n\n'
            "Rules:\n"
            "- Extract people (names), organizations, projects, key concepts\n"
            "- Use simple relationship types: relates_to, works_at, works_on, parent_of, mentions\n"
            "- Only extract what is explicitly stated, do not invent\n"
            "- If nothing meaningful can be extracted, return empty arrays\n\n"
            f"Text: {text[:1000]}"
        )

        response = await client.aio.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

        graph_data = json.loads(response.text)
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        if not nodes and not edges:
            return

        # Create all nodes first and build a label→id map
        label_to_id: dict[str, str] = {}
        for node in nodes:
            label = node.get("label", "").strip()
            if not label:
                continue
            node_id = await ensure_graph_node(
                user_id, label, node.get("type", "concept"),
                metadata={"source": "auto_extract", "memory_id": memory_id},
            )
            if node_id:
                label_to_id[label] = node_id

        # Create edges
        edge_meta = {"memory_id": memory_id} if memory_id else {}
        for edge in edges:
            src_label = edge.get("source", "").strip()
            tgt_label = edge.get("target", "").strip()
            rel = edge.get("relationship", "relates_to")

            src_id = label_to_id.get(src_label)
            tgt_id = label_to_id.get(tgt_label)

            # If a label wasn't in the nodes list, create it on the fly
            if not src_id and src_label:
                src_id = await ensure_graph_node(user_id, src_label)
                if src_id:
                    label_to_id[src_label] = src_id
            if not tgt_id and tgt_label:
                tgt_id = await ensure_graph_node(user_id, tgt_label)
                if tgt_id:
                    label_to_id[tgt_label] = tgt_id

            if src_id and tgt_id:
                await create_graph_edge(user_id, src_id, tgt_id, rel, metadata=edge_meta)

    except Exception as e:
        print(f"[GRAPH EXTRACT ERROR] user={user_id}: {e}")


# ═══════════════════════════════════════════════
# SECTION 4: HYBRID SEARCH (Graph + Vector)
# ═══════════════════════════════════════════════

async def hybrid_search_graph(user_id: str, query: str) -> str:
    """Graph-first search: find a matching node and return its connections.

    Used by brain interrogation and pulse context enrichment.
    Returns a formatted string of graph relationships, or "" if nothing found.
    """
    if not query or not query.strip():
        return ""
    try:
        supabase = await get_supabase()

        # Find primary node by label match
        nodes_res = await supabase.table("graph_nodes") \
            .select("id, label") \
            .eq("user_id", user_id) \
            .ilike("label", f"%{query[:80]}%") \
            .limit(1).execute()

        if not nodes_res.data:
            return ""

        primary = nodes_res.data[0]
        primary_id = primary["id"]

        # Fetch edges connected to this node
        edges_res = await supabase.table("graph_edges") \
            .select("source_node_id, target_node_id, relationship") \
            .eq("user_id", user_id) \
            .or_(f"source_node_id.eq.{primary_id},target_node_id.eq.{primary_id}") \
            .execute()

        if not edges_res.data:
            return ""

        # Collect connected node IDs
        connected_ids: set[str] = set()
        for edge in edges_res.data:
            if edge["source_node_id"] == primary_id:
                connected_ids.add(edge["target_node_id"])
            else:
                connected_ids.add(edge["source_node_id"])

        if not connected_ids:
            return ""

        # Fetch labels for connected nodes
        labels_res = await supabase.table("graph_nodes") \
            .select("id, label") \
            .eq("user_id", user_id) \
            .in_("id", list(connected_ids)) \
            .execute()
        label_map = {n["id"]: n["label"] for n in (labels_res.data or [])}

        # Format as readable graph relationships
        lines: list[str] = []
        for edge in edges_res.data:
            if edge["source_node_id"] == primary_id:
                target_label = label_map.get(edge["target_node_id"], "Unknown")
                lines.append(f"[{primary['label']}] → [{edge['relationship']}] → [{target_label}]")
            else:
                source_label = label_map.get(edge["source_node_id"], "Unknown")
                lines.append(f"[{source_label}] → [{edge['relationship']}] → [{primary['label']}]")

        return "\n".join(lines[:20])  # Cap at 20 relationships to avoid prompt bloat

    except Exception as e:
        print(f"[GRAPH SEARCH ERROR] user={user_id}: {e}")
        return ""


async def interrogate_brain(user_id: str, query: str) -> str:
    """On-demand brain search — combines graph context + vector memory recall.

    Used when user sends "?query" in the chat.
    Returns a formatted AI answer grounded in the user's own data.
    """
    if not query or not query.strip():
        return "Send a question after the `?` to search your vault."

    try:
        # Fire graph + vector search in parallel
        graph_coro = hybrid_search_graph(user_id, query)
        memory_coro = retrieve_memories(user_id, query, top_k=5, threshold=0.45)
        graph_ctx, memories = await asyncio.gather(graph_coro, memory_coro)

        context_parts: list[str] = []
        if graph_ctx:
            context_parts.append(f"KNOWLEDGE GRAPH:\n{graph_ctx}")
        for m in memories:
            source = m.get("memory_type", "memory").upper()
            content = m.get("content", "")
            context_parts.append(f"[{source}] {content}")

        if not context_parts:
            return "🔍 No relevant memories found. Try a different query."

        context_str = "\n\n".join(context_parts)

        client = get_genai_client()
        prompt = (
            "You are the user's knowledge retrieval assistant. "
            "Answer their question using ONLY the context provided below. "
            "If the context doesn't contain the answer, say so honestly. "
            "Be concise and cite the source type (MEMORY, GRAPH, etc.) when possible.\n\n"
            f"CONTEXT:\n{context_str}\n\n"
            f"QUESTION: {query}\n\n"
            "Provide a clear, direct answer."
        )

        response = await client.aio.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
        )
        return response.text.strip()

    except Exception as e:
        print(f"[BRAIN QUERY ERROR] user={user_id}: {e}")
        return "⚠️ Search failed. Try again."


# ═══════════════════════════════════════════════
# SECTION 5: RESOURCE ENRICHMENT
# ═══════════════════════════════════════════════

async def batch_enrich_resources(user_id: str) -> list[dict]:
    """Find unenriched resources and batch-classify them via Gemini.

    Adds: strategic_note, category correction, and embedding.
    Called during pulse processing.
    """
    try:
        supabase = await get_supabase()
        unenriched = await supabase.table("resources") \
            .select("id, url, title, summary") \
            .eq("user_id", user_id) \
            .is_("enriched_at", "null") \
            .limit(10).execute()

        if not unenriched.data:
            return []

        items = unenriched.data
        print(f"[ENRICH] user={user_id}: Found {len(items)} unenriched resources")

        # Build classification prompt
        resource_list = json.dumps(
            [{"id": r["id"], "url": r["url"], "title": r.get("title", ""), "summary": r.get("summary", "")}
             for r in items],
            indent=2,
        )
        prompt = (
            "For each resource below, provide a strategic_note (one sentence on why this matters) "
            "and verify/update the category.\n\n"
            "Categories: GITHUB, ARTICLE, X_THREAD, LINKEDIN, TOOL, COMPETITOR, MARKET_TREND, PERSONAL, LINK\n\n"
            "Return ONLY a valid JSON array:\n"
            '[{"id": 1, "strategic_note": "...", "category": "..."}]\n\n'
            f"Resources:\n{resource_list}"
        )

        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        parsed = json.loads(response.text)

        now_iso = datetime.now(timezone.utc).isoformat()

        for classified in parsed:
            rid = classified.get("id")
            strategic_note = classified.get("strategic_note", "")
            category = classified.get("category", "LINK")

            # Find matching item for embedding text
            original = next((r for r in items if r["id"] == rid), None)
            if not original:
                continue

            embed_text = f"{original.get('title', '')}. {strategic_note}"
            embedding = await get_embedding_async(embed_text)

            await supabase.table("resources").update({
                "strategic_note": strategic_note,
                "category": category,
                "enriched_at": now_iso,
                "embedding": embedding,
            }).eq("id", rid).eq("user_id", user_id).execute()

        print(f"[ENRICH] user={user_id}: Enriched {len(parsed)} resources")
        return parsed

    except Exception as e:
        print(f"[ENRICH ERROR] user={user_id}: {e}")
        return []
