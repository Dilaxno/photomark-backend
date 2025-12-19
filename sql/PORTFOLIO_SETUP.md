# Portfolio Feature Setup Instructions

## Overview
The portfolio feature has been fully implemented in the backend code, but requires database tables to be created.

## Database Migration Required

Run the SQL migration file to create the required tables:

### Option 1: Neon SQL Editor (Recommended)
1. Go to https://console.neon.tech
2. Select your project → SQL Editor
3. Open `backend/sql/28_portfolio_tables.sql`
4. Copy and paste the entire contents
5. Click "Run" to execute

### Option 2: psql CLI
```bash
psql 'your_connection_string' -f backend/sql/28_portfolio_tables.sql
```

### Option 3: Python Script
```python
from core.database import SessionLocal
db = SessionLocal()

# Read and execute the SQL file
with open('backend/sql/28_portfolio_tables.sql', 'r') as f:
    sql = f.read()
    db.execute(sql)
    db.commit()
```

## Tables Created

The migration creates three tables:

### 1. `portfolio_settings`
- Stores portfolio configuration (title, subtitle, template, publish status)
- One record per user (uid is primary key)

### 2. `portfolio_photos`
- Stores portfolio photos with order and metadata
- Multiple photos per user

### 3. `portfolio_domains`
- Stores custom domain configuration for portfolio pages
- Handles DNS verification and SSL status
- One domain per user

## API Endpoints Available

Once tables are created, these endpoints will work:

### Portfolio Management
- `GET /api/portfolio/photos` - Get all portfolio photos
- `POST /api/portfolio/upload` - Upload photos to portfolio
- `POST /api/portfolio/add-from-gallery` - Add existing photos from gallery
- `DELETE /api/portfolio/photos/{photo_id}` - Delete a photo
- `GET /api/portfolio/settings` - Get portfolio settings
- `POST /api/portfolio/settings` - Save portfolio settings
- `POST /api/portfolio/publish` - Publish/unpublish portfolio
- `GET /api/portfolio/{user_id}/public` - Get public portfolio

### Custom Domain Management
- `GET /api/portfolio/domain/config` - Get domain configuration
- `POST /api/portfolio/domain` - Set custom domain
- `POST /api/portfolio/domain/remove` - Remove custom domain
- `POST /api/portfolio/domain/enable` - Enable domain after DNS verification
- `GET /api/portfolio/domain/status` - Check DNS/TLS status
- `GET /api/portfolio/domain/public/{hostname}` - Get portfolio by custom domain

## Custom Domain Setup

Users can configure custom domains for their portfolios:

1. User sets domain via `POST /api/portfolio/domain` with `{"hostname": "portfolio.example.com"}`
2. System returns DNS instructions: CNAME → `photomark.app`
3. User configures DNS with their provider
4. System automatically verifies DNS via `GET /api/portfolio/domain/status`
5. Once verified, Caddy automatically issues SSL certificate
6. Portfolio becomes accessible at custom domain

## Templates Available

Three portfolio templates are supported:
- `canvas` - Grid layout with hover effects (default)
- `editorial` - Magazine-style single column layout
- `noir` - Dark theme with masonry layout

## Verification

After running the migration, verify tables exist:

```sql
-- Check tables exist
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
AND table_name LIKE 'portfolio%';

-- Should return:
-- portfolio_settings
-- portfolio_photos
-- portfolio_domains
```

## Troubleshooting

**Error: relation "portfolio_settings" does not exist**
- The migration hasn't been run yet
- Run the SQL file as described above

**Error: column "user_uid" does not exist in website_domains**
- This is the old error that has been fixed
- The code now uses `portfolio_domains` instead of `website_domains`

**Custom domain not working**
- Check DNS is configured correctly (CNAME → photomark.app)
- Verify DNS with `GET /api/portfolio/domain/status`
- Check domain is enabled in database
- Ensure Caddy configuration includes portfolio domain handling

## Next Steps

After database migration:
1. Test portfolio creation via API
2. Upload test photos
3. Configure custom domain (optional)
4. Verify public portfolio access
5. Test all three templates