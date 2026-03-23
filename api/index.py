from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from .webhook import process_webhook
from .pulse import process_pulse
from .whatsapp import process_whatsapp_webhook
from .auth import handle_google_auth_start, handle_google_auth_callback
from .google_sync import backfill_tasks_to_google
import os

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
