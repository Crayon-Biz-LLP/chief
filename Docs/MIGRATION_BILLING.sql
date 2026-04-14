-- ============================================================
-- CHIEF OS — Billing & Admin Migration
-- Run against your Supabase database (SQL Editor)
-- ============================================================

-- ─────────────────────────────────────────────
-- 1. SUBSCRIPTIONS TABLE
--    One row per user. Controls access.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id           uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id      text NOT NULL UNIQUE,              -- wa_<phone> or telegram chat_id
    plan         text NOT NULL DEFAULT 'trial',      -- trial | pro | unlimited
    status       text NOT NULL DEFAULT 'active',     -- active | expired | suspended
    trial_days   integer NOT NULL DEFAULT 14,        -- configurable per user
    started_at   timestamptz NOT NULL DEFAULT now(),  -- when plan began
    expires_at   timestamptz,                        -- NULL = never expires (unlimited)
    extended_by  text,                               -- admin user_id who last extended
    notes        text,                               -- admin notes
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now()
);

-- Auto-set expires_at for trial users on insert
CREATE OR REPLACE FUNCTION set_trial_expiry()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.plan = 'trial' AND NEW.expires_at IS NULL THEN
        NEW.expires_at := NEW.started_at + (NEW.trial_days || ' days')::interval;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_set_trial_expiry
    BEFORE INSERT ON subscriptions
    FOR EACH ROW
    EXECUTE FUNCTION set_trial_expiry();

-- RLS
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "subscriptions_all" ON subscriptions FOR ALL USING (true) WITH CHECK (true);

-- ─────────────────────────────────────────────
-- 2. USAGE EVENTS TABLE
--    Append-only log of every billable action.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS usage_events (
    id           uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id      text NOT NULL,
    event_type   text NOT NULL,       -- message_in | message_out | pulse | brain_query | research | media_process
    channel      text,                -- whatsapp | telegram
    metadata     jsonb DEFAULT '{}',  -- extra context (e.g. intent, byte count)
    created_at   timestamptz DEFAULT now()
);

-- Index for analytics queries
CREATE INDEX IF NOT EXISTS idx_usage_events_user_date
    ON usage_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_type
    ON usage_events (event_type, created_at DESC);

-- RLS
ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "usage_events_all" ON usage_events FOR ALL USING (true) WITH CHECK (true);

-- ─────────────────────────────────────────────
-- 3. BACKFILL: Create subscription rows for
--    existing users based on core_config.joined_at
-- ─────────────────────────────────────────────
INSERT INTO subscriptions (user_id, plan, status, trial_days, started_at, expires_at)
SELECT 
    cc.user_id,
    'trial',
    CASE 
        WHEN (now() - (cc.content::timestamptz)) > interval '14 days' THEN 'expired'
        ELSE 'active'
    END,
    14,
    cc.content::timestamptz,
    cc.content::timestamptz + interval '14 days'
FROM core_config cc
WHERE cc.key = 'joined_at'
ON CONFLICT (user_id) DO NOTHING;
