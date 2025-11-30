"""
PostgreSQL database connection and setup for Neon
Replaces Firestore as primary data store
"""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load environment variables from project root
# Look for .env in project root: backend/core/database.py -> backend/core -> backend -> Software -> .env
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.env')
load_dotenv(dotenv_path=env_path)

DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required for PostgreSQL connection")

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # Verify connections before using
    pool_size=10,
    max_overflow=20,
    echo=False  # Set to True for SQL query logging in development
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for ORM models
Base = declarative_base()

def get_db():
    """
    Dependency for FastAPI routes to get database session
    Usage:
        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """
    Initialize database tables
    Call this on application startup
    """
    Base.metadata.create_all(bind=engine)
    # Run idempotent DDL inside a transaction so changes are committed
    try:
        with engine.begin() as conn:
            # Ensure shops.domain exists (custom domain config)
            chk = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='shops' AND column_name='domain'"))
            if not chk.first():
                conn.execute(text("ALTER TABLE public.shops ADD COLUMN IF NOT EXISTS domain JSONB NOT NULL DEFAULT '{}'::jsonb"))

            # Ensure customer enrichment columns exist on shop_sales
            chk3 = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='shop_sales' AND column_name='customer_name'"))
            if not chk3.first():
                conn.execute(text("ALTER TABLE public.shop_sales ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)"))
            chk4 = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='shop_sales' AND column_name='customer_city'"))
            if not chk4.first():
                conn.execute(text("ALTER TABLE public.shop_sales ADD COLUMN IF NOT EXISTS customer_city VARCHAR(255)"))
            chk5 = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='shop_sales' AND column_name='customer_country'"))
            if not chk5.first():
                conn.execute(text("ALTER TABLE public.shop_sales ADD COLUMN IF NOT EXISTS customer_country VARCHAR(64)"))

            # Ensure collaborators.name column exists (idempotent)
            chk6 = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='name'"))
            if not chk6.first():
                conn.execute(text("ALTER TABLE public.collaborators ADD COLUMN IF NOT EXISTS name VARCHAR(255)"))

            # Ensure collaborators.last_login_at column exists (idempotent)
            chk7 = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='last_login_at'"))
            if not chk7.first():
                conn.execute(text("ALTER TABLE public.collaborators ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ"))
    except Exception:
        # Swallow to avoid startup crash in constrained envs; logs handled by callers
        pass
