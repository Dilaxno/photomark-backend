-- AUTHORIZED IP ADDRESSES (if auth_ip router persisted IPs)
CREATE TABLE IF NOT EXISTS public.allowed_ips (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  ip_cidr TEXT NOT NULL, -- e.g., '203.0.113.7/32' or '203.0.113.0/24'
  label TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(owner_uid, ip_cidr)
);

CREATE INDEX IF NOT EXISTS ix_allowed_ips_owner ON public.allowed_ips (owner_uid);
