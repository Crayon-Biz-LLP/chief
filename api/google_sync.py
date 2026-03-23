"""
Multi-user Google Calendar & Tasks sync helpers.

All functions take a user_id and resolve credentials from Supabase.
Token refresh is handled automatically.
"""

import os
import httpx
from datetime import datetime, timezone, timedelta
from supabase import create_async_client, AsyncClient

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TASKS_API = "https://www.googleapis.com/tasks/v1"

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY")
        )
    return _supabase_client


# ─────────────────────────────────────────────
# TOKEN MANAGEMENT
# ─────────────────────────────────────────────

async def get_user_access_token(user_id: str) -> str | None:
    """
    Get a valid access_token for a user.
    Automatically refreshes if expired. Returns None if user has no Google connection.
    """
    supabase = await get_supabase()
    res = await supabase.table("user_google_tokens").select("*").eq("user_id", user_id).limit(1).execute()

    if not res.data:
        return None

    token_row = res.data[0]
    access_token = token_row["access_token"]
    refresh_token = token_row["refresh_token"]
    expiry_str = token_row["token_expiry"]

    # Check if token is expired (with 5-min buffer)
    try:
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) + timedelta(minutes=5) < expiry:
            return access_token  # Still valid
    except (ValueError, TypeError):
        pass

    # Token expired — refresh it
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })

        if not resp.is_success:
            print(f"[GOOGLE REFRESH ERROR] User {user_id}: {resp.text}")
            return None

        data = resp.json()
        new_access_token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Update in DB
        await supabase.table("user_google_tokens").update({
            "access_token": new_access_token,
            "token_expiry": new_expiry.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("user_id", user_id).execute()

        print(f"[GOOGLE REFRESH] Refreshed token for {user_id}")
        return new_access_token

    except Exception as e:
        print(f"[GOOGLE REFRESH CRITICAL] User {user_id}: {e}")
        return None


async def has_google_connection(user_id: str) -> bool:
    """Quick check if user has Google tokens stored."""
    supabase = await get_supabase()
    res = await supabase.table("user_google_tokens").select("user_id").eq("user_id", user_id).limit(1).execute()
    return bool(res.data)


# ─────────────────────────────────────────────
# GOOGLE CALENDAR — Create / Update / Delete events
# ─────────────────────────────────────────────

async def sync_to_calendar(user_id: str, title: str, start_iso: str,
                           tz_name: str = "Asia/Kolkata", event_id: str | None = None) -> str | None:
    """
    Create or update a 30-minute calendar event.
    Returns the Google Calendar event_id, or None on failure.
    """
    access_token = await get_user_access_token(user_id)
    if not access_token:
        return None

    try:
        clean_iso = start_iso.replace("Z", "+00:00")
        start_dt = datetime.fromisoformat(clean_iso)
        end_dt = start_dt + timedelta(minutes=30)

        event_body = {
            "summary": f"🔥 {title}",
            "description": "Auto-synced by Chief OS",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
            "reminders": {"useDefault": True},
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            if event_id:
                # Update existing event
                resp = await client.patch(
                    f"{CALENDAR_API}/calendars/primary/events/{event_id}",
                    json=event_body, headers=headers
                )
            else:
                # Create new event
                resp = await client.post(
                    f"{CALENDAR_API}/calendars/primary/events",
                    json=event_body, headers=headers
                )

        if resp.is_success:
            data = resp.json()
            new_id = data.get("id")
            print(f"[CALENDAR] {'Updated' if event_id else 'Created'} event for {user_id}: {title}")
            return new_id
        else:
            print(f"[CALENDAR ERROR] {user_id}: {resp.status_code} {resp.text}")
            # If updating failed (event deleted manually), try creating fresh
            if event_id:
                return await sync_to_calendar(user_id, title, start_iso, tz_name, event_id=None)
            return None

    except Exception as e:
        print(f"[CALENDAR CRITICAL] {user_id}: {e}")
        return None


async def delete_calendar_event(user_id: str, event_id: str):
    """Remove a calendar event when task is completed/cancelled."""
    if not event_id:
        return

    access_token = await get_user_access_token(user_id)
    if not access_token:
        return

    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.delete(
                f"{CALENDAR_API}/calendars/primary/events/{event_id}",
                headers=headers
            )
        print(f"[CALENDAR] Deleted event {event_id} for {user_id}")
    except Exception as e:
        print(f"[CALENDAR DELETE] {user_id}: {e}")


# ─────────────────────────────────────────────
# GOOGLE TASKS — Create / Complete tasks
# ─────────────────────────────────────────────

async def sync_to_google_tasks(user_id: str, title: str,
                                due_at: str | None = None,
                                task_id: str | None = None,
                                status: str = "todo") -> str | None:
    """
    Create, update, or complete a Google Task.
    Returns the Google Tasks task_id, or None on failure.
    """
    access_token = await get_user_access_token(user_id)
    if not access_token:
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Complete/cancel → mark as completed
            if task_id and status in ("done", "cancelled"):
                resp = await client.patch(
                    f"{TASKS_API}/lists/@default/tasks/{task_id}",
                    json={"status": "completed"},
                    headers=headers,
                )
                return task_id if resp.is_success else None

            # Build task body
            body = {}
            if title:
                # Time visibility hack: prepend time to title
                if due_at and "T" in due_at:
                    time_str = due_at.split("T")[1][:5]
                    if time_str not in title:
                        title = f"🕒 {time_str} | {title}"
                body["title"] = title

            if due_at:
                body["due"] = due_at if "T" in due_at else f"{due_at}T09:00:00+00:00"

            if task_id:
                # Update existing
                resp = await client.patch(
                    f"{TASKS_API}/lists/@default/tasks/{task_id}",
                    json=body, headers=headers,
                )
            else:
                # Create new
                resp = await client.post(
                    f"{TASKS_API}/lists/@default/tasks",
                    json=body, headers=headers,
                )

            if resp.is_success:
                data = resp.json()
                new_id = data.get("id")
                print(f"[TASKS] {'Updated' if task_id else 'Created'} task for {user_id}: {title}")
                return new_id
            else:
                print(f"[TASKS ERROR] {user_id}: {resp.status_code} {resp.text}")
                return None

    except Exception as e:
        print(f"[TASKS CRITICAL] {user_id}: {e}")
        return None
