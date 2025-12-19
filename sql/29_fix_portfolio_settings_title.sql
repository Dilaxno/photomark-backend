-- Fix portfolio_settings table - add missing title column if it doesn't exist
-- This handles cases where the table was created before the title column was added

-- Add title column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'title'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN title VARCHAR(255) NOT NULL DEFAULT 'My Portfolio';
        
        RAISE NOTICE 'Added title column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Title column already exists in portfolio_settings table';
    END IF;
END $$;

-- Add subtitle column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'subtitle'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN subtitle VARCHAR(500);
        
        RAISE NOTICE 'Added subtitle column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Subtitle column already exists in portfolio_settings table';
    END IF;
END $$;

-- Add template column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'template'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN template VARCHAR(50) NOT NULL DEFAULT 'canvas';
        
        RAISE NOTICE 'Added template column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Template column already exists in portfolio_settings table';
    END IF;
END $$;

-- Add custom_domain column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'custom_domain'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN custom_domain VARCHAR(255);
        
        RAISE NOTICE 'Added custom_domain column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Custom_domain column already exists in portfolio_settings table';
    END IF;
END $$;

-- Add is_published column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'is_published'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT FALSE;
        
        RAISE NOTICE 'Added is_published column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Is_published column already exists in portfolio_settings table';
    END IF;
END $$;

-- Add published_at column if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        AND column_name = 'published_at'
    ) THEN
        ALTER TABLE portfolio_settings 
        ADD COLUMN published_at TIMESTAMPTZ;
        
        RAISE NOTICE 'Added published_at column to portfolio_settings table';
    ELSE
        RAISE NOTICE 'Published_at column already exists in portfolio_settings table';
    END IF;
END $$;

-- Verify the table structure
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_name = 'portfolio_settings' 
ORDER BY ordinal_position;