"""
Migrate data from Firestore to PostgreSQL (Neon)
Run this after initializing PostgreSQL schema
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore
from sqlalchemy.orm import Session
from core.database import SessionLocal
from models.shop import Shop, ShopSlug
from models.user import User
from datetime import datetime

def init_firebase():
    """Initialize Firebase Admin SDK"""
    # Look for Firebase credentials in project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))  # backend/scripts -> backend -> Software
    cred_path = os.path.join(project_root, os.getenv("FIREBASE_ADMIN_SDK_PATH", "firebase-adminsdk.json"))
    
    if not os.path.exists(cred_path):
        print(f"✗ Firebase credentials not found at {cred_path}")
        sys.exit(1)
    
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    return firestore.client()

def migrate_shops(db: Session, firestore_db):
    """Migrate shops collection from Firestore to PostgreSQL"""
    print("\n=== Migrating Shops ===")
    
    shops_ref = firestore_db.collection('shops')
    docs = shops_ref.stream()
    
    count = 0
    for doc in docs:
        data = doc.to_dict()
        
        # Create Shop record
        shop = Shop(
            uid=doc.id,
            name=data.get('name', 'Untitled Shop'),
            slug=data.get('slug', doc.id),
            description=data.get('description', ''),
            owner_uid=data.get('ownerUid', doc.id),
            owner_name=data.get('ownerName'),
            theme=data.get('theme', {}),
            products=data.get('products', []),
            created_at=data.get('createdAt') if 'createdAt' in data else datetime.utcnow(),
            updated_at=data.get('updatedAt') if 'updatedAt' in data else datetime.utcnow()
        )
        
        # Merge or add
        existing = db.query(Shop).filter(Shop.uid == doc.id).first()
        if existing:
            db.merge(shop)
        else:
            db.add(shop)
        
        count += 1
    
    db.commit()
    print(f"✓ Migrated {count} shops")

def migrate_shop_slugs(db: Session, firestore_db):
    """Migrate shop_slugs collection from Firestore to PostgreSQL"""
    print("\n=== Migrating Shop Slugs ===")
    
    slugs_ref = firestore_db.collection('shop_slugs')
    docs = slugs_ref.stream()
    
    count = 0
    for doc in docs:
        data = doc.to_dict()
        
        slug_mapping = ShopSlug(
            slug=doc.id,
            uid=data.get('uid', ''),
            updated_at=data.get('updatedAt') if 'updatedAt' in data else datetime.utcnow()
        )
        
        existing = db.query(ShopSlug).filter(ShopSlug.slug == doc.id).first()
        if existing:
            db.merge(slug_mapping)
        else:
            db.add(slug_mapping)
        
        count += 1
    
    db.commit()
    print(f"✓ Migrated {count} shop slugs")

def migrate_users(db: Session, firestore_db):
    """Migrate users collection from Firestore to PostgreSQL"""
    print("\n=== Migrating Users ===")
    
    users_ref = firestore_db.collection('users')
    docs = users_ref.stream()
    
    count = 0
    skipped = 0
    for doc in docs:
        data = doc.to_dict()
        
        # Skip users without valid email
        email = data.get('email', '').strip()
        if not email:
            print(f"  ⚠ Skipping user {doc.id} - no email")
            skipped += 1
            continue
        
        user = User(
            uid=doc.id,
            email=email,
            display_name=data.get('displayName'),
            photo_url=data.get('photoUrl'),
            account_type=data.get('accountType', 'individual'),
            referral_source=data.get('referralSource'),
            company_name=data.get('companyName'),
            company_size=data.get('companySize'),
            plan=data.get('plan', 'free'),
            stripe_customer_id=data.get('stripeCustomerId'),
            subscription_status=data.get('subscriptionStatus'),
            storage_used_bytes=data.get('storageUsed', 0),
            storage_limit_bytes=data.get('storageLimit', 1073741824),
            affiliate_code=data.get('affiliateCode'),
            referred_by=data.get('referredBy'),
            affiliate_earnings=data.get('affiliateEarnings', 0.0),
            is_active=data.get('isActive', True),
            email_verified=data.get('emailVerified', False),
            created_at=data.get('createdAt') if 'createdAt' in data else datetime.utcnow(),
            updated_at=data.get('updatedAt') if 'updatedAt' in data else datetime.utcnow(),
            last_login_at=data.get('lastLoginAt'),
            extra_metadata=data.get('metadata', {})
        )
        
        existing = db.query(User).filter(User.uid == doc.id).first()
        if existing:
            db.merge(user)
        else:
            db.add(user)
        
        # Commit each user individually to handle duplicates gracefully
        try:
            db.commit()
            count += 1
        except Exception as e:
            db.rollback()
            print(f"  ⚠ Skipping user {email} - {str(e)[:100]}")
            skipped += 1
    
    print(f"✓ Migrated {count} users ({skipped} skipped)")

def main():
    print("=" * 50)
    print("Firestore → PostgreSQL Migration")
    print("=" * 50)
    
    # Initialize connections
    print("\n[1/4] Connecting to databases...")
    firestore_db = init_firebase()
    postgres_db = SessionLocal()
    print("✓ Connected to Firestore and PostgreSQL")
    
    try:
        # Run migrations
        print("\n[2/4] Migrating data...")
        migrate_shops(postgres_db, firestore_db)
        migrate_shop_slugs(postgres_db, firestore_db)
        migrate_users(postgres_db, firestore_db)
        
        print("\n[3/4] Verifying migration...")
        shop_count = postgres_db.query(Shop).count()
        slug_count = postgres_db.query(ShopSlug).count()
        user_count = postgres_db.query(User).count()
        
        print(f"  - Shops: {shop_count}")
        print(f"  - Shop Slugs: {slug_count}")
        print(f"  - Users: {user_count}")
        
        print("\n[4/4] Migration complete!")
        print("=" * 50)
        print("\n✓ All data migrated successfully!")
        print("\nNext steps:")
        print("  1. Verify data in PostgreSQL")
        print("  2. Update backend routers to use PostgreSQL")
        print("  3. Test thoroughly before deploying")
        
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        postgres_db.rollback()
        sys.exit(1)
    finally:
        postgres_db.close()

if __name__ == "__main__":
    main()
