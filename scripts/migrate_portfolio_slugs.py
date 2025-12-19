"""
Migration script to create portfolio slugs for existing portfolios
"""
import sys
import os

# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_db
from models.portfolio import PortfolioSettings
from models.portfolio_slug import PortfolioSlug
from models.user import User
import re

def slugify(text: str) -> str:
    """Convert text to URL-friendly slug"""
    if not text:
        return 'portfolio'
    
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', text.lower().strip())
    slug = re.sub(r'[\s_-]+', '-', slug)
    slug = slug.strip('-')[:50]  # Limit length
    
    return slug or 'portfolio'

def generate_user_slug(display_name: str = None, email: str = None) -> str:
    """Generate a user-friendly slug from display name or email"""
    if display_name and display_name.strip():
        return slugify(display_name.strip())
    
    if email:
        username = email.split('@')[0]
        return slugify(username)
    
    return 'portfolio'

def migrate_portfolio_slugs():
    """Create slugs for existing portfolios"""
    print("Migrating existing portfolios to use slugs...")
    
    db = next(get_db())
    
    try:
        # Get all existing portfolio settings
        portfolios = db.query(PortfolioSettings).all()
        print(f"Found {len(portfolios)} existing portfolios")
        
        created_count = 0
        
        for portfolio in portfolios:
            # Check if slug already exists
            existing_slug = db.query(PortfolioSlug).filter(
                PortfolioSlug.uid == portfolio.uid
            ).first()
            
            if existing_slug:
                print(f"  ✓ Slug already exists for {portfolio.uid}: {existing_slug.slug}")
                continue
            
            # Use portfolio title first, then fall back to user info
            if portfolio.title and portfolio.title.strip():
                base_slug = slugify(portfolio.title.strip())
                print(f"  → Using portfolio title '{portfolio.title}' for {portfolio.uid}")
            else:
                # Get user info for fallback slug generation
                user = db.query(User).filter(User.uid == portfolio.uid).first()
                display_name = user.display_name if user else None
                email = user.email if user else None
                base_slug = generate_user_slug(display_name, email)
                print(f"  → Using user info for {portfolio.uid} (no portfolio title)")
            
            # Ensure uniqueness
            slug = base_slug
            counter = 1
            while db.query(PortfolioSlug).filter(PortfolioSlug.slug == slug).first():
                slug = f"{base_slug}-{counter}"
                counter += 1
            
            # Create slug record
            portfolio_slug = PortfolioSlug(slug=slug, uid=portfolio.uid)
            db.add(portfolio_slug)
            
            print(f"  ✓ Created slug for {portfolio.uid}: {slug}")
            created_count += 1
        
        db.commit()
        print(f"\n✓ Migration completed! Created {created_count} new slugs")
        
    except Exception as e:
        print(f"✗ Error during migration: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    migrate_portfolio_slugs()