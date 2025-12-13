-- Remove foreign key constraints from affiliate tables
-- This allows attributions and conversions to be created before user records exist
-- (tracking happens before user sync)

-- Drop the existing foreign key constraints from affiliate_attributions
ALTER TABLE public.affiliate_attributions 
DROP CONSTRAINT IF EXISTS affiliate_attributions_user_uid_fkey;

ALTER TABLE public.affiliate_attributions 
DROP CONSTRAINT IF EXISTS affiliate_attributions_affiliate_uid_fkey;

-- Drop the existing foreign key constraints from affiliate_conversions
ALTER TABLE public.affiliate_conversions 
DROP CONSTRAINT IF EXISTS affiliate_conversions_user_uid_fkey;

ALTER TABLE public.affiliate_conversions 
DROP CONSTRAINT IF EXISTS affiliate_conversions_affiliate_uid_fkey;

-- The tables will now allow any user_uid and affiliate_uid values
-- Orphaned records can be cleaned up periodically if needed
