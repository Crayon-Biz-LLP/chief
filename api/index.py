from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from .webhook import process_webhook
from .pulse import process_pulse
from .whatsapp import process_whatsapp_webhook
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
