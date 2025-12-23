-- Add status column to vaults table for persistent vault workflow status
ALTER TABLE public.vaults ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'awaiting_proofing';
ALTER TABLE public.vaults ADD COLUMN IF NOT EXISTS proofing_completed_at TIMESTAMPTZ;
ALTER TABLE public.vaults ADD COLUMN IF NOT EXISTS final_delivery_prepared_at TIMESTAMPTZ;

-- Index for status queries
CREATE INDEX IF NOT EXISTS ix_vaults_status ON public.vaults (status);

-- Update existing vaults to have proper status based on metadata
UPDATE public.vaults 
SET status = CASE 
    WHEN metadata->>'final_delivery' IS NOT NULL AND (metadata->'final_delivery'->>'prepared')::boolean = true THEN 'delivered'
    WHEN metadata->>'proofing_complete' IS NOT NULL AND (metadata->'proofing_complete'->>'completed')::boolean = true THEN 'proofing_completed'
    ELSE 'awaiting_proofing'
END
WHERE status IS NULL OR status = 'awaiting_proofing';
