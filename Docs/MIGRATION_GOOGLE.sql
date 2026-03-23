-- ============================================================
-- Chief OS — Google Calendar/Tasks Integration Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ============================================================

-- 1. NEW TABLE: user_google_tokens
-- Stores per-user OAuth tokens for Google Calendar & Tasks
CREATE TABLE IF NOT EXISTS user_google_tokens (
    user_id        text PRIMARY KEY,         -- wa_919876543210
    access_token   text NOT NULL,
    refresh_token  text NOT NULL,
    token_expiry   timestamptz NOT NULL,
    scopes         text DEFAULT 'calendar.events tasks',
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now()
);

ALTER TABLE user_google_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY "tokens_all" ON user_google_tokens FOR ALL USING (true) WITH CHECK (true);

-- 2. ADD google sync columns to tasks table
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS google_task_id text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS google_event_id text;

-- ============================================================
-- DONE. Run this once, then delete this file.
-- ============================================================
