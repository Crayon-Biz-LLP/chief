"""
Chief OS — Billing & Subscription Engine
=========================================
Centralised access control + usage metering.
Replaces the hardcoded 14-day trial check.

Tables used:
  - subscriptions  (one row per user — plan, status, expires_at)
  - usage_events   (append-only log of every billable action)
"""

import os
from datetime import datetime, timezone
from typing import Optional

from supabase import create_async_client, AsyncClient

# ─────────────────────────────────────────────
# SUPABASE SINGLETON
# ─────────────────────────────────────────────
_supabase: Optional[AsyncClient] = None

async def get_supabase() -> AsyncClient:
    global _supabase
    if _supabase is None:
        _supabase = await create_async_client(
            os.getenv("SUPABASE_URL", ""),
            os.getenv("SUPABASE_ANON_KEY", ""),
        )
    return _supabase


# ─────────────────────────────────────────────
# ACCESS CHECK  (replaces is_trial_expired)
# ─────────────────────────────────────────────

async def check_access(user_id: str) -> dict:
    """
    Returns:
        {
            "allowed": True/False,
            "plan": "trial" | "pro" | "unlimited",
            "status": "active" | "expired" | "suspended",
            "days_left": int or None,
            "reason": str  (only if not allowed)
        }
    """
    supabase = await get_supabase()
    res = await (
        supabase.table("subscriptions")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )

    # No subscription row → auto-create a trial
    if not res.data:
        return await _create_trial(user_id)

    sub = res.data[0]
    plan = sub.get("plan", "trial")
    status = sub.get("status", "active")
    expires_at = sub.get("expires_at")

    # Unlimited plan — always allowed
    if plan == "unlimited" and status == "active":
        return {"allowed": True, "plan": plan, "status": "active", "days_left": None}

    # Suspended by admin
    if status == "suspended":
        return {
            "allowed": False,
            "plan": plan,
            "status": "suspended",
            "days_left": 0,
            "reason": "Account suspended. Contact your admin.",
        }

    # Check expiry
    if expires_at:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        remaining = (exp - now).total_seconds()

        if remaining <= 0:
            # Mark as expired if not already
            if status != "expired":
                await supabase.table("subscriptions").update(
                    {"status": "expired", "updated_at": now.isoformat()}
                ).eq("user_id", user_id).execute()
            return {
                "allowed": False,
                "plan": plan,
                "status": "expired",
                "days_left": 0,
                "reason": "Your plan has expired. Contact your admin to continue.",
            }

        days_left = max(1, int(remaining / 86400))
        return {"allowed": True, "plan": plan, "status": "active", "days_left": days_left}

    # No expiry set + active = allowed
    return {"allowed": True, "plan": plan, "status": "active", "days_left": None}


async def _create_trial(user_id: str) -> dict:
    """Auto-provision a trial subscription for a new user."""
    supabase = await get_supabase()

    # Check if they have a joined_at in core_config
    joined_res = await (
        supabase.table("core_config")
        .select("content")
        .eq("user_id", user_id)
        .eq("key", "joined_at")
        .limit(1)
        .execute()
    )

    now = datetime.now(timezone.utc)
    started = now
    if joined_res.data:
        try:
            started = datetime.fromisoformat(
                joined_res.data[0]["content"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    trial_days = 14
    expires = started + __import__("datetime").timedelta(days=trial_days)

    status = "active" if expires > now else "expired"

    await supabase.table("subscriptions").insert({
        "user_id": user_id,
        "plan": "trial",
        "status": status,
        "trial_days": trial_days,
        "started_at": started.isoformat(),
        "expires_at": expires.isoformat(),
    }).execute()

    if status == "expired":
        return {
            "allowed": False, "plan": "trial", "status": "expired",
            "days_left": 0,
            "reason": "Your trial has expired. Contact your admin to continue.",
        }

    days_left = max(1, int((expires - now).total_seconds() / 86400))
    return {"allowed": True, "plan": "trial", "status": "active", "days_left": days_left}


# ─────────────────────────────────────────────
# USAGE METERING
# ─────────────────────────────────────────────

async def record_usage(
    user_id: str,
    event_type: str,
    channel: str = "",
    metadata: dict | None = None,
):
    """
    Append a usage event.  Fire-and-forget (errors are swallowed).

    event_type values:
        message_in    — user sent a message
        message_out   — bot replied
        pulse         — daily briefing generated
        brain_query   — ? vault interrogation
        research      — research agent task
        media_process — multimodal extraction
    """
    try:
        supabase = await get_supabase()
        await supabase.table("usage_events").insert({
            "user_id": user_id,
            "event_type": event_type,
            "channel": channel or "",
            "metadata": metadata or {},
        }).execute()
    except Exception as e:
        print(f"[BILLING] Usage record failed for {user_id}: {e}")


# ═══════════════════════════════════════════════
# ADMIN API FUNCTIONS
# ═══════════════════════════════════════════════

async def admin_list_users() -> list[dict]:
    """
    Returns all users with subscription + usage stats.
    Joins subscriptions with core_config for names and aggregates usage.
    """
    supabase = await get_supabase()

    # Get all subscriptions
    subs_res = await supabase.table("subscriptions").select("*").order("created_at", desc=True).execute()
    subs = subs_res.data or []

    # Get user names from core_config
    names_res = await (
        supabase.table("core_config")
        .select("user_id, content")
        .eq("key", "user_name")
        .execute()
    )
    name_map = {r["user_id"]: r["content"] for r in (names_res.data or [])}

    # Get joined_at
    joined_res = await (
        supabase.table("core_config")
        .select("user_id, content")
        .eq("key", "joined_at")
        .execute()
    )
    joined_map = {r["user_id"]: r["content"] for r in (joined_res.data or [])}

    # Get mission mode
    mode_res = await (
        supabase.table("core_config")
        .select("user_id, content")
        .eq("key", "mission_mode")
        .execute()
    )
    mode_map = {r["user_id"]: r["content"] for r in (mode_res.data or [])}

    # Get current season/goal
    goal_res = await (
        supabase.table("core_config")
        .select("user_id, content")
        .eq("key", "current_season")
        .execute()
    )
    goal_map = {r["user_id"]: r["content"] for r in (goal_res.data or [])}

    # Get channel from user_id prefix
    users = []
    for s in subs:
        uid = s["user_id"]
        channel = "whatsapp" if uid.startswith("wa_") else "telegram"
        display_id = uid.replace("wa_", "") if channel == "whatsapp" else uid

        now = datetime.now(timezone.utc)
        days_left = None
        if s.get("expires_at"):
            try:
                exp = datetime.fromisoformat(str(s["expires_at"]).replace("Z", "+00:00"))
                remaining = (exp - now).total_seconds()
                days_left = max(0, int(remaining / 86400))
            except (ValueError, TypeError):
                pass

        users.append({
            "user_id": uid,
            "display_id": display_id,
            "name": name_map.get(uid, "—"),
            "channel": channel,
            "plan": s.get("plan", "trial"),
            "status": s.get("status", "active"),
            "trial_days": s.get("trial_days", 14),
            "days_left": days_left,
            "started_at": s.get("started_at"),
            "expires_at": s.get("expires_at"),
            "joined_at": joined_map.get(uid),
            "mission_mode": mode_map.get(uid, "—"),
            "goal": goal_map.get(uid, "—"),
            "notes": s.get("notes", ""),
        })

    return users


async def admin_get_user_detail(user_id: str) -> dict:
    """Full user profile + usage breakdown."""
    supabase = await get_supabase()

    # Subscription
    sub_res = await supabase.table("subscriptions").select("*").eq("user_id", user_id).limit(1).execute()
    sub = sub_res.data[0] if sub_res.data else {}

    # Config
    config_res = await supabase.table("core_config").select("key, content").eq("user_id", user_id).execute()
    config = {r["key"]: r["content"] for r in (config_res.data or [])}

    # Task counts
    tasks_res = await supabase.table("tasks").select("status", count="exact").eq("user_id", user_id).execute()
    active_res = await supabase.table("tasks").select("id", count="exact").eq("user_id", user_id).eq("status", "todo").execute()
    done_res = await supabase.table("tasks").select("id", count="exact").eq("user_id", user_id).eq("status", "done").execute()

    # Dump counts
    dumps_res = await supabase.table("raw_dumps").select("id", count="exact").eq("user_id", user_id).execute()

    # Memory count
    mem_res = await supabase.table("memories").select("id", count="exact").eq("user_id", user_id).execute()

    # Usage summary (last 30 days)
    thirty_days_ago = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=30)).isoformat()
    usage_res = await (
        supabase.table("usage_events")
        .select("event_type")
        .eq("user_id", user_id)
        .gte("created_at", thirty_days_ago)
        .execute()
    )
    usage_counts = {}
    for ev in (usage_res.data or []):
        et = ev.get("event_type", "unknown")
        usage_counts[et] = usage_counts.get(et, 0) + 1

    return {
        "user_id": user_id,
        "subscription": sub,
        "config": config,
        "stats": {
            "total_tasks": tasks_res.count if hasattr(tasks_res, "count") else len(tasks_res.data or []),
            "active_tasks": active_res.count if hasattr(active_res, "count") else len(active_res.data or []),
            "completed_tasks": done_res.count if hasattr(done_res, "count") else len(done_res.data or []),
            "total_dumps": dumps_res.count if hasattr(dumps_res, "count") else len(dumps_res.data or []),
            "total_memories": mem_res.count if hasattr(mem_res, "count") else len(mem_res.data or []),
        },
        "usage_30d": usage_counts,
    }


async def admin_update_subscription(
    user_id: str,
    plan: str | None = None,
    status: str | None = None,
    add_days: int | None = None,
    set_expires: str | None = None,
    notes: str | None = None,
    admin_user: str = "admin",
) -> dict:
    """
    Update a user's subscription.  Supports:
      - plan change (trial → pro → unlimited)
      - status change (active / suspended / expired)
      - add_days: extend current expiry by N days
      - set_expires: set absolute expiry (ISO timestamp)
      - notes: admin annotation
    """
    supabase = await get_supabase()
    now = datetime.now(timezone.utc)

    # Ensure subscription exists
    sub_res = await supabase.table("subscriptions").select("*").eq("user_id", user_id).limit(1).execute()
    if not sub_res.data:
        return {"error": f"No subscription found for {user_id}"}

    current = sub_res.data[0]
    updates = {"updated_at": now.isoformat(), "extended_by": admin_user}

    if plan:
        updates["plan"] = plan
        if plan == "unlimited":
            updates["expires_at"] = None
            updates["status"] = "active"

    if status:
        updates["status"] = status

    if add_days and add_days > 0:
        current_expiry = current.get("expires_at")
        if current_expiry:
            try:
                exp = datetime.fromisoformat(str(current_expiry).replace("Z", "+00:00"))
                # If already expired, extend from now
                base = max(exp, now)
            except (ValueError, TypeError):
                base = now
        else:
            base = now
        new_expiry = base + __import__("datetime").timedelta(days=add_days)
        updates["expires_at"] = new_expiry.isoformat()
        updates["status"] = "active"

    if set_expires:
        updates["expires_at"] = set_expires
        try:
            exp = datetime.fromisoformat(set_expires.replace("Z", "+00:00"))
            if exp > now:
                updates["status"] = "active"
        except (ValueError, TypeError):
            pass

    if notes is not None:
        updates["notes"] = notes

    await supabase.table("subscriptions").update(updates).eq("user_id", user_id).execute()

    # Return updated record
    final = await supabase.table("subscriptions").select("*").eq("user_id", user_id).limit(1).execute()
    return final.data[0] if final.data else {"success": True}


async def admin_get_analytics() -> dict:
    """
    Platform-wide analytics for the admin dashboard.
    """
    supabase = await get_supabase()
    now = datetime.now(timezone.utc)

    # Total users
    subs_res = await supabase.table("subscriptions").select("user_id, plan, status").execute()
    subs = subs_res.data or []

    total_users = len(subs)
    active_users = sum(1 for s in subs if s["status"] == "active")
    expired_users = sum(1 for s in subs if s["status"] == "expired")
    suspended_users = sum(1 for s in subs if s["status"] == "suspended")

    plan_breakdown = {}
    for s in subs:
        p = s.get("plan", "trial")
        plan_breakdown[p] = plan_breakdown.get(p, 0) + 1

    # Usage last 7 days
    seven_days_ago = (now - __import__("datetime").timedelta(days=7)).isoformat()
    usage_res = await (
        supabase.table("usage_events")
        .select("event_type, user_id, created_at")
        .gte("created_at", seven_days_ago)
        .execute()
    )
    events = usage_res.data or []

    total_events_7d = len(events)
    event_breakdown = {}
    active_users_7d = set()
    daily_counts = {}
    for ev in events:
        et = ev.get("event_type", "unknown")
        event_breakdown[et] = event_breakdown.get(et, 0) + 1
        active_users_7d.add(ev.get("user_id"))
        day = ev.get("created_at", "")[:10]
        daily_counts[day] = daily_counts.get(day, 0) + 1

    # Usage last 30 days for trend
    thirty_days_ago = (now - __import__("datetime").timedelta(days=30)).isoformat()
    usage_30d_res = await (
        supabase.table("usage_events")
        .select("created_at")
        .gte("created_at", thirty_days_ago)
        .execute()
    )
    daily_30d = {}
    for ev in (usage_30d_res.data or []):
        day = ev.get("created_at", "")[:10]
        daily_30d[day] = daily_30d.get(day, 0) + 1

    return {
        "total_users": total_users,
        "active_users": active_users,
        "expired_users": expired_users,
        "suspended_users": suspended_users,
        "plan_breakdown": plan_breakdown,
        "usage_7d": {
            "total_events": total_events_7d,
            "active_users": len(active_users_7d),
            "event_breakdown": event_breakdown,
            "daily": dict(sorted(daily_counts.items())),
        },
        "usage_30d_daily": dict(sorted(daily_30d.items())),
    }
