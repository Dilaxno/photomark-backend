#!/usr/bin/env python3
"""
Fix portfolio_settings table schema by adding missing columns.
Run this script to fix the database schema issue.
"""
import os
import sys
import asyncio
from pathlib import Path

# Add the backend directory to the Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from core.database import get_db
from sqlalchemy import text

def run_migration():
    """Run the portfolio schema fix migration"""
    try:
        # Read the migration SQL
        migration_file = backend_dir / "sql" / "29_fix_portfolio_settings_title.sql"
        with open(migration_file, 'r') as f:
            migration_sql = f.read()
        
        # Get database connection
        db = next(get_db())
        
        print("Running portfolio_settings schema fix migration...")
        
        # Execute the migration
        result = db.execute(text(migration_sql))
        db.commit()
        
        print("✅ Migration completed successfully!")
        print("The portfolio_settings table now has all required columns.")
        
        # Verify the schema
        print("\nVerifying table structure:")
        verify_sql = """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns 
        WHERE table_name = 'portfolio_settings' 
        ORDER BY ordinal_position;
        """
        
        columns = db.execute(text(verify_sql)).fetchall()
        for col in columns:
            print(f"  - {col.column_name}: {col.data_type} {'NULL' if col.is_nullable == 'YES' else 'NOT NULL'}")
        
        db.close()
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)