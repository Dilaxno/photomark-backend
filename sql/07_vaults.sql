-- VAULTS (if vault metadata was previously stored in Firestore)
CREATE TABLE IF NOT EXISTS public.vaults (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  visibility TEXT DEFAULT 'private',  -- private, shared, public
  metadata JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_vaults_owner ON public.vaults (owner_uid);
CREATE INDEX IF NOT EXISTS ix_vaults_visibility ON public.vaults (visibility);
