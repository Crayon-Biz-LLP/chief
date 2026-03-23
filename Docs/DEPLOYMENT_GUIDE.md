# Chief OS: Deployment & Connection Guide (v2)

This document provides a comprehensive end-to-end guide on configuring, deploying, and tying together the systems that power the **Chief OS Digital 2iC (Python Engine)**.

## Architecture Overview
The system operates as a Serverless Python API hosted on **Vercel**. It uses **Supabase** for PostgreSQL database state management and **Google Gemini 2.5 Flash** for AI synthesis. Interaction is handled by webhooks connected to **Meta WhatsApp Cloud API** and **Telegram Bot API**. Periodic AI Briefings (Pulse) are triggered securely by **GitHub Actions Cron**.

**v2 Changes:** Auto-timezone detection, 3-step onboarding, mission modes, URL enrichment, resource capture, WhatsApp interactive menus.

**v3 Changes:** Google Calendar & Tasks integration with per-user OAuth 2.0.

---

## 1. Supabase Database Setup
Supabase serves as the nervous system for the OS.

### Core Tables (Required):
*   `core_config` — User settings KV store (mission_mode, pulse_schedule, timezone_offset, current_season, etc.)
*   `raw_dumps` — User's unstructured thoughts; marked with `is_processed=FALSE`
*   `tasks` — Structured actionable items extracted by AI (includes `is_revenue_critical`, `reminder_at`, `deadline`)
*   `projects` — User's project categories with `org_tag` for routing
*   `people` — Stakeholders auto-detected from user dumps
*   `logs` — AI-generated logs (IDEAS, SPARK, etc.)

### New Tables (v2):
*   `resources` — URLs/links captured from dumps (category, summary, project/mission links)
*   `missions` — Strategic goals auto-detected from patterns

### New Tables (v3 — Google Integration):
*   `user_google_tokens` — Per-user OAuth 2.0 tokens (access_token, refresh_token, token_expiry, scopes)
*   `tasks` columns added: `google_task_id` (text), `google_event_id` (text)

> Run `Docs/MIGRATION_GOOGLE.sql` in Supabase SQL Editor to add the v3 schema.

> Full schema reference: [`Docs/SCHEMA.md`](./SCHEMA.md)

**Connection Details needed for Vercel:**
*   `SUPABASE_URL`: Your Supabase Project URL.
*   `SUPABASE_ANON_KEY`: Your Supabase Project API Key.

---

## 2. Vercel Deployment & Environment Variables
Vercel hosts the serverless Python functions that process incoming webhook events from messaging platforms and the pulse triggers from GitHub.

1.  Connect your Vercel Project to your `main` branch on GitHub.
2.  Set the **Build Command** to empty (or default) and ensure the runtime relies on `vercel.json` targeting `api/index.py`.
3.  Inject the following Environment Variables into the Vercel Production Environment:
    *   `SUPABASE_URL`
    *   `SUPABASE_ANON_KEY`
    *   `GEMINI_API_KEY`
    *   `TELEGRAM_BOT_TOKEN` (From BotFather)
    *   `TELEGRAM_WEBHOOK_SECRET` (Custom secret phrase used to verify incoming Telegram webhooks)
    *   `WHATSAPP_ACCESS_TOKEN` (From Meta App Dashboard -> API Setup)
    *   `WHATSAPP_VERIFY_TOKEN` (Custom string used in Meta webhook verification)
    *   `WHATSAPP_PHONE_NUMBER_ID` (From Meta App Dashboard -> API Setup — *Crucial for Pulse delivery*)
    *   `PULSE_SECRET` (A custom strong password to protect the `/api/pulse` endpoint)
    *   `INVITE_CODE` (The invite code users must enter to activate — default: `chief2026`)
    *   `ADMIN_CHAT_ID` (Optional — Telegram chat ID for error alerts)
    *   `GOOGLE_CLIENT_ID` (Google OAuth 2.0 Client ID — from Google Cloud Console)
    *   `GOOGLE_CLIENT_SECRET` (Google OAuth 2.0 Client Secret — from Google Cloud Console)

*Wait for Vercel to generate your production URL: e.g., `https://chief-three.vercel.app`.*

---

## 3. Webhook Connections (Messaging Apps)

### WhatsApp (Meta Cloud API)
1.  Go to your Meta App Dashboard -> WhatsApp -> Configuration.
2.  Set the Webhook Callback URL to: `https://[YOUR_VERCEL_URL]/api/whatsapp/webhook`
3.  Set the Verify Token to match exactly what is in your `api/index.py` (e.g., `chiefos_secure_wa_webhook_123`).
4.  Subscribe to the `messages` webhook field.
5.  *Development Mode Note:* Since the app is in DevelopmentMode, any receiving phone number must be added to the verified test list in the API Setup page, otherwise Meta throws error `131030`.

### Telegram
1. Run a script or a `curl` command once to register your Vercel URL with Telegram:
   ```bash
   curl -F "url=https://[YOUR_VERCEL_URL]/api/telegram/webhook" \
        -F "secret_token=[YOUR_TELEGRAM_WEBHOOK_SECRET]" \
        "https://api.telegram.org/bot[TELEGRAM_BOT_TOKEN]/setWebhook"
   ```

---

## 4. GitHub Actions (The Pulse Engine)
Vercel goes to sleep when not actively queried. We use GitHub Actions to run a cron job that reliably wakes the server up every hour to generate intelligence briefings.

1.  In your GitHub Repository, navigate to **Settings -> Secrets and variables -> Actions**.
2.  Create a Repository Secret:
    *   Name: `TRIAL_PULSE_SECRET`
    *   Secret: `[Exact same password you put in Vercel PULSE_SECRET]`
3.  The `.github/workflows/pulse.yml` will automatically trigger a `POST /api/pulse` request, injecting this secret into the `x-pulse-secret` header.

---

## 5. Google Calendar & Tasks Integration (v3)
Enables per-user two-way sync between Chief tasks and Google Calendar + Google Tasks.

### Google Cloud Setup:
1.  Go to [Google Cloud Console](https://console.cloud.google.com/) → Create or select a project (e.g., "chief OS").
2.  **Enable APIs:** Enable `Google Calendar API` and `Google Tasks API`.
3.  **OAuth Consent Screen:** Configure as External, add scopes: `calendar.events`, `tasks`. Add your email as a test user.
4.  **Credentials:** Create an OAuth 2.0 Client ID (Web application type):
    *   **Authorized redirect URI:** `https://[YOUR_VERCEL_URL]/api/auth/google/callback`
5.  Copy the Client ID and Client Secret into Vercel env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.

### Database Migration:
Run `Docs/MIGRATION_GOOGLE.sql` in Supabase SQL Editor before deploying.

### How It Works:
1.  After onboarding (or via *settings* → *📅 Connect Google*), user receives an OAuth link.
2.  User taps link → Google consent screen → grants Calendar & Tasks permission.
3.  Callback stores tokens in `user_google_tokens` table.
4.  During each Pulse cycle:
    *   **New tasks** → Created as Google Tasks + 30-min Calendar events (if task has a time).
    *   **Completed tasks** → Marked complete in Google Tasks + Calendar event removed.
5.  Token refresh is automatic — `get_user_access_token()` refreshes expired tokens using the stored `refresh_token`.

### OAuth Routes:
*   `GET /api/auth/google?user=wa_xxx` → Redirects to Google consent
*   `GET /api/auth/google/callback?code=xxx&state=wa_xxx` → Exchanges code for tokens, shows success page

---

## 6. End-to-End Application Flow (v2)

### Phase 1: WhatsApp Onboarding (3 Steps)
1.  New user sends any message → Gatekeeper prompts for invite code.
2.  User sends the correct `INVITE_CODE` → Access granted.
3.  **Timezone auto-detected** from phone number country code (e.g., +91 → GMT+5.5). Zero friction.
4.  **Step 1: Mission Mode** → User selects FIX / GROW / BUILD / REST (WhatsApp List Message).
5.  **Step 2: Schedule** → User selects Early / Standard / Late (WhatsApp Buttons).
6.  **Step 3: Goal** → User types their 14-day goal as free text.
7.  Activation message confirms config. User is now live.

### Phase 2: Active User — Commands & Capture
*   **Capture Mode:** Any normal text → stored in `raw_dumps`. Bot replies "Captured." LLM is NOT invoked (saves cost/latency).
*   **Command Menu:** Type `menu`, `help`, or `commands` → WhatsApp List Message with all actions.
*   **Direct Commands:** Type `urgent`, `brief`, `goal`, `vault`, `people`, or `settings`.
*   **Settings:** Change schedule, mode, goal, or override timezone via interactive menus.
*   **Reset:** Type `start` or `reset` → confirmation button → full config wipe + re-onboarding.

### Phase 3: The Pulse (AI Generation)
1.  GitHub Actions pings `https://[YOUR_VERCEL_URL]/api/pulse` every hour with `x-pulse-secret`.
2.  Vercel queries `core_config` for all users, checks schedule vs local time (auto-detected timezone).
3.  For each scheduled user, pulls: unread `raw_dumps`, active `tasks`, `projects`, `people`.
4.  **URL Enrichment:** Any URLs in dumps are scraped for og:title/description metadata.
5.  **Mission Mode** shapes the AI persona (FIX=crisis manager, GROW=sales strategist, BUILD=product engineer, REST=wellbeing coach).
6.  Gemini 2.5 Flash generates JSON: `briefing`, `new_tasks`, `completed_task_ids`, `new_projects`, `new_people`, `resources`, `logs`.
7.  Database writes: tasks created/completed, projects/people auto-onboarded, resources saved, dumps marked processed.
8.  Briefing routed to WhatsApp or Telegram based on `user_id` prefix (`wa_` or `tg_`).
9.  Race condition lock: 30-min cooldown per user prevents duplicate pulses.
