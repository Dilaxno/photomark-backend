-- PRICING / SUBSCRIPTIONS EVENTS (generic store for webhook/audit)
CREATE TABLE IF NOT EXISTS public.pricing_events (
  id SERIAL PRIMARY KEY,
  user_uid TEXT REFERENCES public.users(uid) ON DELETE SET NULL,
  provider TEXT NOT NULL DEFAULT 'dodo',
  event_type TEXT NOT NULL,
  event_id TEXT,
  payload JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_pricing_events_user ON public.pricing_events (user_uid);
CREATE INDEX IF NOT EXISTS ix_pricing_events_type ON public.pricing_events (event_type);
CREATE INDEX IF NOT EXISTS ix_pricing_events_created ON public.pricing_events (created_at);

-- ACTIVE SUBSCRIPTIONS SNAPSHOT (optional)
CREATE TABLE IF NOT EXISTS public.subscriptions (
  id SERIAL PRIMARY KEY,
  user_uid TEXT NOT NULL UNIQUE REFERENCES public.users(uid) ON DELETE CASCADE,
  provider TEXT NOT NULL DEFAULT 'dodo',
  plan TEXT NOT NULL,
  status TEXT NOT NULL,
  current_period_end TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_subscriptions_plan ON public.subscriptions (plan);
CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON public.subscriptions (status);
