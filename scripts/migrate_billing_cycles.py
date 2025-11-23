"""
Migration script to backfill billing_cycle for existing paid users.
Run this once to update all users who have a paid plan but no billing_cycle.
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.auth import get_fs_client
from core.config import logger

try:
    from firebase_admin import firestore as fb_fs
except Exception:
    fb_fs = None


def migrate_billing_cycles(dry_run: bool = True, default_cycle: str = 'yearly'):
    """
    Backfill billing_cycle for paid users who don't have it set.
    
    Args:
        dry_run: If True, only log what would be changed without making changes
        default_cycle: Default billing cycle to assign ('monthly' or 'yearly')
    """
    db = get_fs_client()
    if not db or not fb_fs:
        logger.error("Firestore is not available")
        return
    
    logger.info("=" * 60)
    logger.info(f"Starting billing cycle migration (dry_run={dry_run})")
    logger.info(f"Default cycle: {default_cycle}")
    logger.info("=" * 60)
    
    # Query all users with paid plans
    paid_plans = ['individual', 'studios', 'photographers', 'agencies', 'pro', 'team']
    
    updated_count = 0
    skipped_count = 0
    error_count = 0
    
    try:
        # Get all users
        users_ref = db.collection('users')
        users = users_ref.stream()
        
        for user_doc in users:
            uid = user_doc.id
            data = user_doc.to_dict() or {}
            
            plan = str(data.get('plan', '')).lower().strip()
            current_billing = data.get('billing_cycle') or data.get('billingCycle')
            is_paid = data.get('isPaid', False)
            
            # Check if user is on a paid plan but missing billing_cycle
            if plan in paid_plans and is_paid and not current_billing:
                logger.info(f"Found user without billing_cycle: uid={uid} plan={plan}")
                
                if not dry_run:
                    try:
                        # Update with both field names for compatibility
                        users_ref.document(uid).update({
                            'billing_cycle': default_cycle,
                            'billingCycle': default_cycle,
                            'updatedAt': fb_fs.SERVER_TIMESTAMP,
                        })
                        logger.info(f"✅ Updated {uid}: set billing_cycle={default_cycle}")
                        updated_count += 1
                    except Exception as ex:
                        logger.error(f"❌ Failed to update {uid}: {ex}")
                        error_count += 1
                else:
                    logger.info(f"[DRY RUN] Would update {uid}: set billing_cycle={default_cycle}")
                    updated_count += 1
            else:
                # User already has billing_cycle or is on free plan
                if current_billing:
                    logger.debug(f"Skipped {uid}: already has billing_cycle={current_billing}")
                skipped_count += 1
        
        logger.info("=" * 60)
        logger.info("Migration completed!")
        logger.info(f"Updated: {updated_count}")
        logger.info(f"Skipped: {skipped_count}")
        logger.info(f"Errors: {error_count}")
        logger.info("=" * 60)
        
        if dry_run:
            logger.info("This was a DRY RUN. No changes were made.")
            logger.info("Run with dry_run=False to apply changes.")
        
    except Exception as ex:
        logger.error(f"Migration failed: {ex}")
        raise


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Migrate billing cycles for existing paid users')
    parser.add_argument('--live', action='store_true', help='Actually apply changes (default is dry run)')
    parser.add_argument('--cycle', type=str, default='yearly', choices=['monthly', 'yearly'],
                       help='Default billing cycle to assign (default: yearly)')
    
    args = parser.parse_args()
    
    dry_run = not args.live
    
    migrate_billing_cycles(dry_run=dry_run, default_cycle=args.cycle)
