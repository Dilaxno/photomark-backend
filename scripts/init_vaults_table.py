"""
Initialize vaults table in PostgreSQL (Neon)
Run this to create the vaults table for storing vault metadata
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import Base, engine
from models.vaults import Vault

def init_vaults_table():
    """Create vaults table in the database"""
    print("Creating vaults table in PostgreSQL (Neon)...")
    
    try:
        # Create the vaults table
        Vault.__table__.create(bind=engine, checkfirst=True)
        print("✓ Vaults table created successfully!")
        
        # Add unique constraint on uid + vault_name if not exists
        with engine.connect() as conn:
            try:
                from sqlalchemy import text
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_vaults_uid_name ON vaults(uid, vault_name)"
                ))
                conn.commit()
                print("✓ Unique index on (uid, vault_name) created")
            except Exception as idx_ex:
                print(f"Note: Index may already exist: {idx_ex}")
        
        print("\n" + "="*50)
        print("Vaults table is ready!")
        print("="*50)
        print("\nVault metadata will now be stored in PostgreSQL:")
        print("  - logo_url")
        print("  - welcome_message")
        print("  - protection settings")
        print("  - share customization")
        print("  - licensing info")
        print("  - slideshow data (in JSON metadata field)")
        print("\nPhoto files remain in R2 storage.")
        
    except Exception as e:
        print(f"✗ Error creating vaults table: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    init_vaults_table()
