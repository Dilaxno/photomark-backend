-- Vault Trash and Version History Tables
-- Provides soft-delete (trash) and point-in-time recovery for vaults

-- Trash table for soft-deleted vaults
CREATE TABLE IF NOT EXISTS public.vault_trash (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  vault_name TEXT NOT NULL,
  display_name TEXT,
  original_keys JSONB NOT NULL DEFAULT '[]',  -- Array of photo keys
  vault_metadata JSONB NOT NULL DEFAULT '{}',       -- Original vault metadata (renamed from 'metadata' - reserved in SQLAlchemy)
  photo_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes BIGINT NOT NULL DEFAULT 0,
  deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),  -- Auto-purge after 30 days
  restored_at TIMESTAMPTZ,  -- NULL if not restored
  UNIQUE(owner_uid, vault_name, deleted_at)
);

CREATE INDEX IF NOT EXISTS ix_vault_trash_owner ON public.vault_trash (owner_uid);
CREATE INDEX IF NOT EXISTS ix_vault_trash_expires ON public.vault_trash (expires_at) WHERE restored_at IS NULL;

-- Vault version snapshots for point-in-time recovery
CREATE TABLE IF NOT EXISTS public.vault_versions (
  id SERIAL PRIMARY KEY,
  owner_uid TEXT NOT NULL REFERENCES public.users(uid) ON DELETE CASCADE,
  vault_name TEXT NOT NULL,
  version_number INTEGER NOT NULL DEFAULT 1,
  snapshot_keys JSONB NOT NULL DEFAULT '[]',  -- Array of photo keys at this version
  vault_metadata JSONB NOT NULL DEFAULT '{}',       -- Vault metadata at this version (renamed from 'metadata' - reserved in SQLAlchemy)
  photo_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  description TEXT,  -- Optional description (e.g., "Before bulk delete", "Auto-snapshot")
  UNIQUE(owner_uid, vault_name, version_number)
);

CREATE INDEX IF NOT EXISTS ix_vault_versions_owner ON public.vault_versions (owner_uid);
CREATE INDEX IF NOT EXISTS ix_vault_versions_vault ON public.vault_versions (owner_uid, vault_name);
CREATE INDEX IF NOT EXISTS ix_vault_versions_created ON public.vault_versions (created_at);

-- Function to auto-increment version number
CREATE OR REPLACE FUNCTION get_next_vault_version(p_owner_uid TEXT, p_vault_name TEXT)
RETURNS INTEGER AS $$
DECLARE
  next_ver INTEGER;
BEGIN
  SELECT COALESCE(MAX(version_number), 0) + 1 INTO next_ver
  FROM public.vault_versions
  WHERE owner_uid = p_owner_uid AND vault_name = p_vault_name;
  RETURN next_ver;
END;
$$ LANGUAGE plpgsql;

-- Cleanup job: Remove expired trash items (run via cron or scheduled task)
-- DELETE FROM public.vault_trash WHERE expires_at < NOW() AND restored_at IS NULL;
