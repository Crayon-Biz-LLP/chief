from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from .webhook import process_webhook
from .pulse import process_pulse
from .whatsapp import process_whatsapp_webhook
from .auth import handle_google_auth_start, handle_google_auth_callback
from .google_sync import backfill_tasks_to_google
from .research import process_all_research
from .billing import (
    admin_list_users,
    admin_get_user_detail,
    admin_update_subscription,
    admin_get_analytics,
)
import os
from pathlib import Path

app = FastAPI(title="Integrated-OS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "Integrated OS API is running on Python 🐍"}

@app.post("/api/webhook")
async def webhook_route(request: Request):
    update = await request.json()
    await process_webhook(update)
    return {"success": True}

@app.post("/api/pulse")
async def pulse_route_post(request: Request):
    secret = request.headers.get("x-pulse-secret")
    env_secret = os.getenv("PULSE_SECRET")
    
    # NEW LOGGING FOR DEBUGGING
    if not env_secret:
        print("[AUTH ERROR] Vercel Environment Variable 'PULSE_SECRET' is MISSING or EMPTY!")
    elif not secret:
        print("[AUTH ERROR] GitHub Action did not send the 'x-pulse-secret' header!")
    elif secret != env_secret:
        print(f"[AUTH ERROR] Secret mismatch! Received length: {len(secret)}, Expected length: {len(env_secret)}")
        
    if secret != env_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    is_manual_trigger = request.headers.get("x-manual-trigger") == 'true'
    await process_pulse(is_manual_trigger)
    return {"success": True}

@app.get("/api/whatsapp/webhook")
async def verify_whatsapp_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        # Meta requires a plain integer response for the challenge
        from fastapi import Response
        return Response(content=challenge, media_type="text/plain")
    
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/api/whatsapp/webhook")
async def receive_whatsapp_webhook(request: Request):
    update = await request.json()
    await process_whatsapp_webhook(update)
    return {"success": True}

# ─────────────────────────────────────────────
# GOOGLE OAUTH ROUTES
# ─────────────────────────────────────────────

@app.get("/api/auth/google")
async def google_auth_start(request: Request):
    """User taps link in WhatsApp → redirects to Google consent screen."""
    user_id = request.query_params.get("user")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user parameter")

    auth_url = await handle_google_auth_start(user_id)
    if not auth_url:
        raise HTTPException(status_code=400, detail="Invalid user")

    return RedirectResponse(url=auth_url)

@app.get("/api/auth/google/callback")
async def google_auth_callback(request: Request):
    """Google redirects here after user grants permission."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(content=f"<h1>Authorization denied</h1><p>{error}</p>", status_code=400)

    html = await handle_google_auth_callback(code, state)
    return HTMLResponse(content=html)

@app.get("/api/auth/google/backfill")
async def google_backfill(request: Request):
    """One-time sync: push all existing active tasks to Google Tasks + Calendar."""
    user_id = request.query_params.get("user")
    secret = request.query_params.get("secret")
    resync = request.query_params.get("resync", "").lower() == "true"
    env_secret = os.getenv("PULSE_SECRET")

    if not user_id or secret != env_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await backfill_tasks_to_google(user_id, resync=resync)
    return result


# ─────────────────────────────────────────────
# RESEARCH AGENT ROUTE
# ─────────────────────────────────────────────

@app.post("/api/research")
async def research_route(request: Request):
    """Cron-triggered: process pending research tasks in agent_queue."""
    secret = request.headers.get("x-pulse-secret")
    env_secret = os.getenv("PULSE_SECRET")

    if secret != env_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = await process_all_research()
    return {"success": True, "processed": results}


# ─────────────────────────────────────────────
# ADMIN PANEL & API
# ─────────────────────────────────────────────

def _verify_admin(request: Request):
    """Validate admin key from header or query param."""
    key = request.headers.get("x-admin-key") or request.query_params.get("key")
    admin_key = os.getenv("ADMIN_SECRET")
    if not admin_key:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET not configured")
    if key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


@app.get("/admin")
async def admin_panel(request: Request):
    """Serve the admin dashboard HTML."""
    html_path = Path(__file__).resolve().parent.parent / "admin.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    raise HTTPException(status_code=404, detail="Admin panel not found")


@app.get("/api/admin/users")
async def admin_users_route(request: Request):
    """List all users with subscription info."""
    _verify_admin(request)
    users = await admin_list_users()
    return users


@app.get("/api/admin/users/{user_id:path}")
async def admin_user_detail_route(user_id: str, request: Request):
    """Get full user detail with usage stats."""
    _verify_admin(request)
    detail = await admin_get_user_detail(user_id)
    return detail


@app.put("/api/admin/users/{user_id:path}")
async def admin_user_update_route(user_id: str, request: Request):
    """Update a user's subscription (plan, status, extend, notes)."""
    _verify_admin(request)
    body = await request.json()
    result = await admin_update_subscription(
        user_id=user_id,
        plan=body.get("plan"),
        status=body.get("status"),
        add_days=body.get("add_days"),
        set_expires=body.get("set_expires"),
        notes=body.get("notes"),
    )
    return result


@app.get("/api/admin/analytics")
async def admin_analytics_route(request: Request):
    """Platform-wide analytics."""
    _verify_admin(request)
    data = await admin_get_analytics()
    return data
