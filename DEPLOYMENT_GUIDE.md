# Integrated-OS: Deployment & Connection Guide

This document provides a comprehensive end-to-end guide on configuring, deploying, and tying together the systems that power the **Chief OS Digital 2iC (Python Engine)**.

## Architecture Overview
The system operates as a Serverless Python API hosted on **Vercel**. It uses **Supabase** for PostgreSQL database state management and **Google Gemini** for AI synthesis. Interaction is handled by webhooks connected to **Meta WhatsApp Cloud API** and **Telegram Bot API**. Periodic AI Briefings (Pulse) are triggered securely by **GitHub Actions Cron**.

---

## 1. Supabase Database Setup
Supabase serves as the nervous system for the OS.

You must have the following tables initialized:
*   `core_config` (User settings, joined timestamps, identity, schedules, mission, anchors)
*   `raw_dumps` (User's unstructured thoughts and messages; marked with `is_processed=FALSE`)
*   `tasks` (Structured actionable items extracted by the LLM)
*   `people` (Known stakeholders from the onboarding anchors)

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
    *   `WHATSAPP_PHONE_NUMBER_ID` (From Meta App Dashboard -> API Setup - *Crucial for the automated Pulse*)
    *   `PULSE_SECRET` (A custom strong password to protect the `/api/pulse` endpoint).

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

## 5. End-to-End Application Flow

### Phase 1: Onboarding (Account Setup)
1.  User sends `Initialize` or `Start` to the WhatsApp or Telegram bot.
2.  The webhook endpoint receives this, wipes any existing database configs for the user, and triggers **Step 1: Persona Selection** (Boss/Partner/Friend).
3.  User taps the interactive button response. The webhook records this to `core_config` and triggers **Step 2: Schedule**.
4.  User answers Schedule, Timezone (e.g., `5.5`), Mission, and Anchors.
5.  Upon Completion, the user is marked as fully calibrated.

### Phase 2: Capture Mode (Raw Storage)
1.  Once Setup is complete, any normal text message sent by the user (e.g., "Need to fire Jim tomorrow" or "Idea: new SaaS app") simply triggers Capture Mode.
2.  The webhook receives the payload and writes it directly to the `raw_dumps` table linked to their `user_id`.
3.  The bot replies instantly with a simple ✅ to acknowledge storage. The LLM is **not** invoked here to save costs and latency.

### Phase 3: The Pulse (AI Generation)
1.  At the top of the hour (HH:30 UTC), GitHub Actions pings `https://[YOUR_VERCEL_URL]/api/pulse` with the master secret.
2.  Vercel queries `core_config` for all users and checks if the current hour matches their elected `pulse_schedule` (adjusted by their `timezone_offset`).
3.  For scheduled users, it pulls all their unread `raw_dumps` and active `tasks`.
4.  These are aggregated and pushed to the **Gemini 2.5 Flash** model with a hardcoded high-density Executive Markdown prompt.
5.  Gemini returns a synthesized JSON containing:
    *   The `briefing` (markdown string)
    *   `new_tasks` (to be inserted into the tasks table)
    *   `completed_task_ids` (to be marked as done based on the user's latest dumps).
6.  The `raw_dumps` are marked as `is_processed=TRUE`.
7.  The final formatted briefing is pushed outward via the correct Meta or Telegram REST API to the user's phone.
