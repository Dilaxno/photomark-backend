-- REPLIES / COMMENTS
CREATE TABLE IF NOT EXISTS public.replies (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  target_id TEXT NOT NULL,               -- asset/post/resource id
  target_type TEXT DEFAULT 'asset',      -- optional categorization
  body TEXT NOT NULL,
  parent_id INTEGER REFERENCES public.replies(id) ON DELETE CASCADE,
  is_deleted BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_replies_owner ON public.replies (owner_uid);
CREATE INDEX IF NOT EXISTS ix_replies_target ON public.replies (target_id);
CREATE INDEX IF NOT EXISTS ix_replies_parent ON public.replies (parent_id);
CREATE INDEX IF NOT EXISTS ix_replies_created ON public.replies (created_at);
