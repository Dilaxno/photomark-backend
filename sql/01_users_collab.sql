-- USERS
CREATE TABLE IF NOT EXISTS public.users (
  uid TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  display_name TEXT,
  photo_url TEXT,

  account_type TEXT DEFAULT 'individual',
  referral_source TEXT,
  company_name TEXT,
  company_size TEXT,
  company_revenue TEXT,

  plan TEXT DEFAULT 'free',
  stripe_customer_id TEXT,
  subscription_id TEXT,
  subscription_status TEXT,
  subscription_end_date TIMESTAMPTZ,

  storage_used_bytes INTEGER DEFAULT 0,
  storage_limit_bytes INTEGER DEFAULT 1073741824, -- 1GB
  monthly_uploads INTEGER DEFAULT 0,
  monthly_upload_limit INTEGER DEFAULT 100,

  affiliate_code TEXT UNIQUE,
  referred_by TEXT,
  affiliate_earnings DOUBLE PRECISION DEFAULT 0.0,

  is_active BOOLEAN DEFAULT TRUE,
  is_admin BOOLEAN DEFAULT FALSE,
  email_verified BOOLEAN DEFAULT FALSE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login_at TIMESTAMPTZ,

  extra_metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_users_email ON public.users (email);
CREATE INDEX IF NOT EXISTS ix_users_stripe_customer_id ON public.users (stripe_customer_id);
CREATE INDEX IF NOT EXISTS ix_users_referred_by ON public.users (referred_by);

-- COLLABORATOR ACCESS
CREATE TABLE IF NOT EXISTS public.collaborator_access (
  id SERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  role TEXT DEFAULT 'viewer',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS ix_collab_owner ON public.collaborator_access (owner_uid);
CREATE INDEX IF NOT EXISTS ix_collab_email ON public.collaborator_access (email);
