"""
Google OAuth 2.0 flow for multi-user Calendar & Tasks integration.

Flow:
  1. User taps link in WhatsApp → GET /api/auth/google?user=wa_919876543210
  2. Server redirects to Google consent screen
  3. User grants permission → Google redirects to GET /api/auth/google/callback?code=xxx&state=wa_xxx
  4. Server exchanges code for tokens → stores in Supabase → shows success page
"""

import os
import httpx
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from supabase import create_async_client, AsyncClient

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/tasks"

_supabase_client: AsyncClient | None = None

async def get_supabase() -> AsyncClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = await create_async_client(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY")
        )
    return _supabase_client


def get_redirect_uri() -> str:
    """Build the callback URL from the stable production domain."""
    # VERCEL_URL gives deployment-specific URLs (e.g. chief-rdnwdcdd4-xxx.vercel.app)
    # which change every deploy and won't match Google's authorized redirect URIs.
    # Use a dedicated env var or fall back to the known production alias.
    base = os.getenv("APP_URL", "https://chief-three.vercel.app")
    if not base.startswith("http"):
        base = f"https://{base}"
    return f"{base}/api/auth/google/callback"


def build_google_auth_url(user_id: str) -> str:
    """Generate the Google OAuth consent URL for a specific user."""
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": get_redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",       # Get refresh_token
        "prompt": "consent",            # Always show consent to get refresh_token
        "state": user_id,               # Pass user_id through the OAuth flow
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def handle_google_auth_start(user_id: str) -> str:
    """Called from GET /api/auth/google?user=wa_xxx. Returns redirect URL."""
    if not user_id:
        return None
    return build_google_auth_url(user_id)


async def handle_google_auth_callback(code: str, state: str) -> str:
    """
    Called from GET /api/auth/google/callback?code=xxx&state=wa_xxx.
    Exchanges auth code for tokens, stores in Supabase.
    Returns an HTML success/error page.
    """
    user_id = state
    if not code or not user_id:
        return _error_page("Missing authorization code or user ID.")

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": get_redirect_uri(),
                "grant_type": "authorization_code",
            })

        if not resp.is_success:
            print(f"[GOOGLE AUTH ERROR] Token exchange failed: {resp.text}")
            return _error_page("Google rejected the authorization. Please try again.")

        data = resp.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in", 3600)

        if not access_token or not refresh_token:
            print(f"[GOOGLE AUTH ERROR] Missing tokens in response: {data}")
            return _error_page("Could not get tokens from Google. Please try again.")

        token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Store tokens in Supabase
        supabase = await get_supabase()

        # Upsert: delete old row if exists, then insert fresh
        await supabase.table("user_google_tokens").delete().eq("user_id", user_id).execute()
        await supabase.table("user_google_tokens").insert([{
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_expiry": token_expiry.isoformat(),
            "scopes": SCOPES,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]).execute()

        # Mark user as Google-connected in core_config
        await supabase.table("core_config").delete().eq("user_id", user_id).eq("key", "google_connected").execute()
        await supabase.table("core_config").insert([{
            "user_id": user_id,
            "key": "google_connected",
            "content": "true",
        }]).execute()

        print(f"[GOOGLE AUTH] Tokens saved for {user_id}")

        # Send WhatsApp confirmation if it's a WhatsApp user
        if user_id.startswith("wa_"):
            try:
                phone_number = user_id[3:]
                phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
                wa_url = f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"
                headers = {
                    "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
                    "Content-Type": "application/json",
                }
                async with httpx.AsyncClient(timeout=10.0) as wa_client:
                    await wa_client.post(wa_url, json={
                        "messaging_product": "whatsapp",
                        "to": phone_number,
                        "type": "text",
                        "text": {"body": "🤖 ✅ *Google connected!*\n\nYour calendar and tasks are now synced. New tasks will automatically appear on your Google Calendar and Tasks list."},
                    }, headers=headers)
            except Exception as e:
                print(f"[GOOGLE AUTH] WhatsApp notification failed: {e}")

        return _success_page()

    except Exception as e:
        print(f"[GOOGLE AUTH CRITICAL] {e}")
        return _error_page("Something went wrong. Please try again from WhatsApp.")


def _success_page() -> str:
    return """<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connected!</title>
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #fff;
               display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
        .card { text-align: center; padding: 40px; max-width: 400px; }
        .icon { font-size: 64px; margin-bottom: 20px; }
        h1 { font-size: 24px; margin-bottom: 12px; }
        p { color: #888; font-size: 16px; line-height: 1.5; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✅</div>
        <h1>Google Connected!</h1>
        <p>Your calendar and tasks are now synced with Chief.<br>You can close this window and go back to WhatsApp.</p>
    </div>
</body>
</html>"""


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connection Failed</title>
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #fff;
               display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
        .card {{ text-align: center; padding: 40px; max-width: 400px; }}
        .icon {{ font-size: 64px; margin-bottom: 20px; }}
        h1 {{ font-size: 24px; margin-bottom: 12px; }}
        p {{ color: #888; font-size: 16px; line-height: 1.5; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">❌</div>
        <h1>Connection Failed</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""
