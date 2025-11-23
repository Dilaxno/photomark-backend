-- PRELAUNCH / WAITLIST SIGNUPS
CREATE TABLE IF NOT EXISTS public.prelaunch_signups (
  id SERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  source TEXT,
  ip TEXT,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(email)
);

CREATE INDEX IF NOT EXISTS ix_prelaunch_email ON public.prelaunch_signups (email);
CREATE INDEX IF NOT EXISTS ix_prelaunch_created ON public.prelaunch_signups (created_at);
