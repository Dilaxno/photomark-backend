"""
Update existing portfolio slugs to use portfolio titles instead of user names
"""
import sys
import os

# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_db
from models.portfolio import PortfolioSettings
from models.portfolio_slug import PortfolioSlug
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

def update_portfolio_slugs():
    """Update existing portfolio slugs to use portfolio titles"""
    print("Updating portfolio slugs to use portfolio titles...")
    
    db = next(get_db())
    
    try:
        # Get all existing portfolio settings with slugs
        portfolios = db.query(PortfolioSettings).all()
        print(f"Found {len(portfolios)} portfolios")
        
        updated_count = 0
        
        for portfolio in portfolios:
            # Get existing slug
            existing_slug = db.query(PortfolioSlug).filter(
                PortfolioSlug.uid == portfolio.uid
            ).first()
            
            if not existing_slug:
                print(f"  ⚠ No slug found for {portfolio.uid}, skipping")
                continue
            
            # Check if portfolio has a meaningful title
            if not portfolio.title or portfolio.title.strip() in ['My Portfolio', 'portfolio']:
                print(f"  → Keeping existing slug for {portfolio.uid}: {existing_slug.slug} (generic title)")
                continue
            
            # Generate new slug from portfolio title
            new_base_slug = slugify(portfolio.title.strip())
            
            # Check if current slug is already based on the title
            if existing_slug.slug.startswith(new_base_slug):
                print(f"  ✓ Slug already matches title for {portfolio.uid}: {existing_slug.slug}")
                continue
            
            # Ensure new slug is unique
            new_slug = new_base_slug
            counter = 1
            while True:
                # Check if this slug exists for a different user
                conflict = db.query(PortfolioSlug).filter(
                    PortfolioSlug.slug == new_slug,
                    PortfolioSlug.uid != portfolio.uid
                ).first()
                
                if not conflict:
                    break
                    
                new_slug = f"{new_base_slug}-{counter}"
                counter += 1
            
            # Update the slug
            old_slug = existing_slug.slug
            existing_slug.slug = new_slug
            
            print(f"  ✓ Updated slug for '{portfolio.title}': {old_slug} → {new_slug}")
            updated_count += 1
        
        db.commit()
        print(f"\n✓ Update completed! Updated {updated_count} slugs")
        
    except Exception as e:
        print(f"✗ Error during update: {e}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    update_portfolio_slugs()