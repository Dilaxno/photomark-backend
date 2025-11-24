-- SHOPS
CREATE TABLE IF NOT EXISTS public.shops (
  uid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  description TEXT,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  owner_name TEXT,

  theme JSONB NOT NULL DEFAULT jsonb_build_object(
    'primaryColor','#3b82f6',
    'secondaryColor','#8b5cf6',
    'accentColor','#f59e0b',
    'backgroundColor','#ffffff',
    'textColor','#1f2937',
    'fontFamily','Inter',
    'logoUrl', NULL,
    'bannerUrl', NULL
  ),

  products JSONB NOT NULL DEFAULT '[]'::jsonb,

  domain JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_shops_slug ON public.shops (slug);
CREATE INDEX IF NOT EXISTS ix_shops_owner_uid ON public.shops (owner_uid);

-- SHOP SLUGS (slug -> uid mapping)
CREATE TABLE IF NOT EXISTS public.shop_slugs (
  slug TEXT PRIMARY KEY,
  uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_shop_slugs_uid ON public.shop_slugs (uid);
