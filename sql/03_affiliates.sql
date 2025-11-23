-- AFFILIATE PROFILES
CREATE TABLE IF NOT EXISTS public.affiliate_profiles (
  uid TEXT PRIMARY KEY REFERENCES public.users(uid) ON DELETE CASCADE,

  platform TEXT,
  channel TEXT,
  email TEXT,
  name TEXT,

  referral_code TEXT NOT NULL UNIQUE,
  referral_link TEXT NOT NULL,

  clicks_total INTEGER DEFAULT 0,
  signups_total INTEGER DEFAULT 0,
  conversions_total INTEGER DEFAULT 0,
  gross_cents_total INTEGER DEFAULT 0,
  payout_cents_total INTEGER DEFAULT 0,

  last_click_at TIMESTAMPTZ,
  last_signup_at TIMESTAMPTZ,
  last_conversion_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_aff_profiles_refcode ON public.affiliate_profiles (referral_code);

-- AFFILIATE ATTRIBUTIONS (one per user)
CREATE TABLE IF NOT EXISTS public.affiliate_attributions (
  id SERIAL PRIMARY KEY,
  affiliate_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  user_uid TEXT NOT NULL UNIQUE REFERENCES public.users(uid) ON DELETE CASCADE,
  ref TEXT,
  verified BOOLEAN DEFAULT FALSE,
  attributed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  verified_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_aff_attrib_affiliate ON public.affiliate_attributions (affiliate_uid);
CREATE INDEX IF NOT EXISTS ix_aff_attrib_verified ON public.affiliate_attributions (verified);
CREATE INDEX IF NOT EXISTS ix_aff_attrib_attributed_at ON public.affiliate_attributions (attributed_at);

-- AFFILIATE CONVERSIONS
CREATE TABLE IF NOT EXISTS public.affiliate_conversions (
  id SERIAL PRIMARY KEY,
  affiliate_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  user_uid TEXT REFERENCES public.users(uid) ON DELETE SET NULL,
  amount_cents INTEGER DEFAULT 0,
  payout_cents INTEGER DEFAULT 0,
  currency TEXT DEFAULT 'usd',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  conversion_date TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_aff_conversions_affiliate ON public.affiliate_conversions (affiliate_uid);
CREATE INDEX IF NOT EXISTS ix_aff_conversions_created_at ON public.affiliate_conversions (created_at);
