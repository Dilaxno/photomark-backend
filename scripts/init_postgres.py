"""
Initialize PostgreSQL database schema
Creates all tables defined in models
"""
import sys
import os

# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import Base, engine
from models.shop import Shop, ShopSlug
from models.user import User
from models.affiliates import AffiliateProfile, AffiliateAttribution, AffiliateConversion
from models.pricing import PricingEvent, Subscription
from models.replies import Reply
from models.prelaunch import PrelaunchSignup
from models.collaborator import Collaborator
from models.portfolio import PortfolioPhoto, PortfolioSettings
from models.portfolio_slug import PortfolioSlug

def init_database():
    """Create all tables in the database"""
    print("Creating PostgreSQL tables...")
    
    try:
        Base.metadata.create_all(bind=engine)
        print("✓ Tables created successfully!")
        print("\nCreated tables:")
        print("  - shops")
        print("  - shop_slugs")
        print("  - users")
        print("  - affiliate_profiles")
        print("  - affiliate_attributions")
        print("  - affiliate_conversions")
        print("  - pricing_events")
        print("  - subscriptions")
        print("  - replies")
        print("  - prelaunch_signups")
        print("  - collaborators")
        print("  - portfolio_photos")
        print("  - portfolio_settings")
        print("  - portfolio_slugs")
        
    except Exception as e:
        print(f"✗ Error creating tables: {e}")
        sys.exit(1)

if __name__ == "__main__":
    init_database()
