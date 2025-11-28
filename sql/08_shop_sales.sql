CREATE TABLE IF NOT EXISTS public.shop_sales (
  id TEXT PRIMARY KEY,
  owner_uid TEXT NOT NULL,
  shop_uid TEXT REFERENCES public.shops(uid) ON DELETE SET NULL,
  slug TEXT,
  payment_id TEXT UNIQUE,
  customer_email TEXT,
  currency TEXT NOT NULL DEFAULT 'USD',
  amount_cents INTEGER NOT NULL DEFAULT 0,
  items JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  delivered BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_shop_sales_owner_uid ON public.shop_sales (owner_uid);
CREATE INDEX IF NOT EXISTS ix_shop_sales_shop_uid ON public.shop_sales (shop_uid);
CREATE INDEX IF NOT EXISTS ix_shop_sales_slug ON public.shop_sales (slug);
CREATE INDEX IF NOT EXISTS ix_shop_sales_created ON public.shop_sales (created_at);
