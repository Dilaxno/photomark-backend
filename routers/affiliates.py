from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
import os
from datetime import datetime

from core.auth import get_uid_from_request
from core.config import logger
from utils.emailing import render_email, send_email_smtp
from utils.storage import read_json_key, write_json_key
from sqlalchemy.orm import Session
from core.database import get_db
from models.affiliates import AffiliateProfile, AffiliateAttribution, AffiliateConversion
from models.user import User

# Firestore dependency fully removed in Neon migration.

def _update_affiliate_profile_fs(affiliate_uid: str, stats: dict):
    """No-op in Neon migration (previously mirrored to Firestore)."""
    return

router = APIRouter(prefix="/api/affiliates", tags=["affiliates"]) 


def _stats_key(affiliate_uid: str) -> str:
    return f"affiliates/{affiliate_uid}/stats.json"


def _attrib_key(user_uid: str) -> str:
    # Which affiliate referred this user
    return f"affiliates/attributions/{user_uid}.json"


def _extract_affiliate_uid(ref_code: str) -> str | None:
    # Our ref codes are either "<slug>-<uid>" or just "<uid>"
    rc = (ref_code or "").strip()
    if not rc:
        return None
    parts = rc.split("-")
    cand = parts[-1]
    return cand or None


@router.get("/ping")
async def affiliates_ping(request: Request):
    """Quick check that the affiliates router is mounted and reachable."""
    client_ip = request.client.host if request.client else "?"
    logger.info(f"[affiliates.ping] from={client_ip}")
    return {"ok": True}


@router.post("/invite")
async def affiliates_invite(request: Request, email: str = Body(..., embed=True), channel: str = Body("", embed=True)):
    # Require authenticated user to prevent abuse
    uid = get_uid_from_request(request)
    client_ip = request.client.host if request.client else "?"
    logger.info(f"[affiliates.invite] start uid={uid or '-'} ip={client_ip} email={email} channel={channel}")

    if not uid:
        logger.warning(f"[affiliates.invite] unauthorized ip={client_ip}")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = (email or "").strip()
    if not email or "@" not in email:
        logger.warning(f"[affiliates.invite] invalid-email uid={uid} email={email}")
        return JSONResponse({"error": "Valid email required"}, status_code=400)

    try:
        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")

        # Compose email content (plain, non-promotional tone)
        safe_channel = (channel or "").strip()
        subject = "Collaboration Proposal"

        # Build HTML using the partner-ready copy
        intro_html = (
            f"Hi{(' ' + safe_channel) if safe_channel else ''},<br><br>"
            f"I wanted to personally introduce you to <b>{app_name}</b> — a platform for photographers, designers, and digital artists to manage, protect, and deliver their work efficiently.<br><br>"
            f"{app_name} lets you:<br>"
            f"<ul>"
            f"<li>Bulk watermark images</li>"
            f"<li>Apply creative style transformations in batches</li>"
            f"<li>Convert image formats at scale</li>"
            f"<li>Host work in a secure, private cloud gallery</li>"
            f"</ul>"
            f"You can also create password-protected vaults for clients, embed galleries into your site, and collaborate with teammates easily.<br><br>"
            f"I believe your audience would find real value in this, which is why I’d love to invite you to join our 40% affiliate partnership. We offer:<br>"
            f"<ul>"
            f"<li>Fast weekly payouts</li>"
            f"<li>A custom dashboard to track earnings</li>"
            f"<li>A product that solves practical problems for creative communities</li>"
            f"</ul>"
            f"If this sounds interesting, you can explore {app_name} here: <a href=\"{front}\">{front}</a><br><br>"
            f"Looking forward to your thoughts!<br><br>"
            f"Best regards,<br>"
            f"Marouane"
        )

        html = render_email(
            "email_basic.html",
            title="Collaboration Proposal",
            intro=intro_html,
            button_label="Explore Photomark",
            button_url="https://photomark.cloud",
        )

        text = (
            f"Hi{(' ' + safe_channel) if safe_channel else ''},\n\n"
            f"I wanted to personally introduce you to {app_name} — a platform for photographers, designers, and digital artists to manage, protect, and deliver their work efficiently.\n\n"
            f"{app_name} lets you:\n"
            f"- Bulk watermark images\n"
            f"- Apply creative style transformations in batches\n"
            f"- Convert image formats at scale\n"
            f"- Host work in a secure, private cloud gallery\n\n"
            f"You can also create password-protected vaults for clients, embed galleries into your site, and collaborate with teammates easily.\n\n"
            f"I believe your audience would find real value in this, which is why I’d love to invite you to join our 40% affiliate partnership. We offer:\n"
            f"- Fast weekly payouts\n"
            f"- A custom dashboard to track earnings\n"
            f"- A product that solves practical problems for creative communities\n\n"
            f"If this sounds interesting, you can explore {app_name} here: {front}\n\n"
            f"Looking forward to your thoughts!\n\n"
            f"Best regards,\n"
            f"Marouane\n"
        )

        logger.info(f"[affiliates.invite] sending to={email} uid={uid}")
        ok = send_email_smtp(
            email,
            subject,
            html,
            text,
            from_addr=os.getenv("MAIL_FROM_AFFILIATES", "affiliates@photomark.cloud"),
            reply_to=os.getenv("REPLY_TO_AFFILIATES", "affiliates@photomark.cloud"),
            from_name=os.getenv("MAIL_FROM_NAME_AFFILIATES", "Photomark Partnerships"),
        )
        if not ok:
            logger.error(f"[affiliates.invite] smtp-failed to={email}")
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        logger.info(f"[affiliates.invite] success to={email} uid={uid}")
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.invite] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/register")
async def affiliates_register(request: Request, platform: str = Body(..., embed=True), channel: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Finalize affiliate registration, persist profile, and send welcome email with referral link."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        # Read existing profile from PostgreSQL
        existing = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        
        def _slugify(val: str) -> str:
            s = (val or '').lower()
            out = []
            prev_dash = False
            for ch in s:
                if ('a' <= ch <= 'z') or ('0' <= ch <= '9'):
                    out.append(ch)
                    prev_dash = False
                else:
                    if not prev_dash:
                        out.append('-')
                    prev_dash = True
            return ''.join(out).strip('-')

        base = _slugify(channel)
        referral_code = (existing.referral_code if existing else None) or (f"{base}-{uid}" if base and len(base) >= 3 else uid)
        referral_link = (existing.referral_link if existing else None) or f"{front}/?ref={referral_code}"

        # Fetch user's email/name from PostgreSQL users table
        email = None
        name = None
        try:
            u = db.query(User).filter(User.uid == uid).first()
            if u:
                email = u.email
                name = u.display_name or (u.email.split('@')[0] if u.email else None)
        except Exception:
            pass

        # Persist affiliate profile in PostgreSQL
        if existing:
            existing.platform = platform
            existing.channel = channel
            existing.email = email
            existing.name = name
            existing.referral_code = referral_code
            existing.referral_link = referral_link
        else:
            profile = AffiliateProfile(
                uid=uid,
                platform=platform,
                channel=channel,
                email=email,
                name=name,
                referral_code=referral_code,
                referral_link=referral_link,
            )
            db.add(profile)
        db.commit()

        # No mirror needed; left as no-op for backward compatibility

        # Send welcome email
        email_sent = False
        if email:
            app_name = os.getenv("APP_NAME", "Photomark")
            subject = "Welcome to Photomark Affiliates"
            intro_html = (
                f"Welcome to <b>{app_name}</b> Affiliates!<br><br>"
                f"Your referral link:<br>"
                f"<a href=\"{referral_link}\">{referral_link}</a><br><br>"
                f"Share it in your content to start earning."
            )
            html = render_email(
                "email_basic.html",
                title="You're in!",
                intro=intro_html,
                button_label="Open Affiliate Dashboard",
                button_url=f"{front}/#affiliate-dashboard",
            )
            text = (
                f"Welcome to {app_name} Affiliates!\n\n"
                f"Your referral link:\n{referral_link}\n\n"
                f"Open your dashboard: {front}/#affiliate-dashboard"
            )
            email_sent = send_email_smtp(
                email,
                subject,
                html,
                text,
                from_addr=os.getenv("MAIL_FROM_AFFILIATES", "affiliates@photomark.cloud"),
                reply_to=os.getenv("REPLY_TO_AFFILIATES", "affiliates@photomark.cloud"),
                from_name=os.getenv("MAIL_FROM_NAME_AFFILIATES", "Photomark Partnerships"),
            )
            if not email_sent:
                logger.error(f"[affiliates.register] welcome-email-failed uid={uid} email={email}")

        return {"ok": True, "referralCode": referral_code, "referralLink": referral_link, "emailSent": bool(email_sent)}
    except Exception as ex:
        logger.exception(f"[affiliates.register] error: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/click")
async def affiliates_track_click(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Record a click for a referral code. Public endpoint."""
    ref = str(payload.get("ref") or "").strip()
    uid = _extract_affiliate_uid(ref)
    if not uid:
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    try:
        # Update JSON mirror (optional)
        stats = read_json_key(_stats_key(uid)) or {}
        stats["clicks"] = int(stats.get("clicks") or 0) + 1
        stats["last_click_at"] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(uid), stats)

        # Update PostgreSQL aggregate
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        if prof:
            prof.clicks_total = int(prof.clicks_total or 0) + 1
            prof.last_click_at = datetime.utcnow()
            db.commit()
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.click] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/signup")
async def affiliates_track_signup(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Record attribution but DO NOT increment signup until verification."""
    ref = str(payload.get("ref") or "").strip()
    new_user_uid = str(payload.get("new_user_uid") or "").strip()
    logger.info(f"[affiliates.track.signup] ref={ref} new_user_uid={new_user_uid}")
    if not ref or not new_user_uid:
        logger.warning(f"[affiliates.track.signup] missing fields ref={ref} new_user_uid={new_user_uid}")
        return JSONResponse({"error": "missing fields"}, status_code=400)
    affiliate_uid = _extract_affiliate_uid(ref)
    logger.info(f"[affiliates.track.signup] extracted affiliate_uid={affiliate_uid} from ref={ref}")
    if not affiliate_uid:
        logger.warning(f"[affiliates.track.signup] invalid ref={ref}")
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    
    # Always store in JSON first (no FK constraints)
    try:
        write_json_key(_attrib_key(new_user_uid), {
            "affiliate_uid": affiliate_uid,
            "attributed_at": datetime.utcnow().isoformat(),
            "ref": ref,
            "verified": False,
        })
        logger.info(f"[affiliates.track.signup] stored in JSON for user={new_user_uid}")
    except Exception as ex:
        logger.warning(f"[affiliates.track.signup] JSON write failed: {ex}")
    
    # Try to persist in PostgreSQL (may fail if user doesn't exist yet due to FK constraint)
    try:
        existing = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == new_user_uid).first()
        if existing:
            logger.info(f"[affiliates.track.signup] updating existing attribution for user={new_user_uid}")
            existing.affiliate_uid = affiliate_uid
            existing.ref = ref
            existing.verified = False
            existing.attributed_at = datetime.utcnow()
            existing.verified_at = None
        else:
            logger.info(f"[affiliates.track.signup] creating new attribution for user={new_user_uid} affiliate={affiliate_uid}")
            db.add(AffiliateAttribution(
                affiliate_uid=affiliate_uid,
                user_uid=new_user_uid,
                ref=ref,
                verified=False,
            ))
        db.commit()
        logger.info(f"[affiliates.track.signup] DB success user={new_user_uid} affiliate={affiliate_uid}")
    except Exception as ex:
        db.rollback()
        # FK constraint failure is expected if user doesn't exist yet
        # The JSON storage will be used as fallback, and DB record will be created in signup_verified
        logger.warning(f"[affiliates.track.signup] DB insert failed (expected if user not synced yet): {ex}")
    
    return {"ok": True}


@router.post("/track/signup_verified")
async def affiliates_track_signup_verified(request: Request, db: Session = Depends(get_db)):
    """After email verification, increment signup for the authenticated user if attributed.
    
    IMPORTANT: This should only increment signups_total ONCE per user.
    We do NOT increment the counter here anymore - instead we count verified attributions
    directly in the stats endpoint. This prevents double-counting issues.
    """
    uid = get_uid_from_request(request)
    logger.info(f"[affiliates.track.signup_verified] uid={uid}")
    if not uid:
        logger.warning("[affiliates.track.signup_verified] no uid from request")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # Expire cache to get fresh data
        db.expire_all()
        
        # Check DB attribution first
        attrib_db = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == uid).first()
        affiliate_uid = attrib_db.affiliate_uid if attrib_db else None
        
        # Fallback to JSON attribution
        attrib = read_json_key(_attrib_key(uid)) or {}
        logger.info(f"[affiliates.track.signup_verified] attrib_db={attrib_db is not None} affiliate_uid={affiliate_uid} json_attrib={attrib}")
        
        if not affiliate_uid:
            affiliate_uid = attrib.get('affiliate_uid')
        
        if not affiliate_uid:
            logger.info(f"[affiliates.track.signup_verified] no attribution found for user={uid}")
            return {"ok": True, "tracked": False, "reason": "no_attribution"}
        
        # Prevent double counting - check if already verified in DB
        if attrib_db and attrib_db.verified:
            logger.info(f"[affiliates.track.signup_verified] already verified in DB for user={uid}")
            return {"ok": True, "tracked": False, "reason": "already_verified"}
        
        # Mark as verified in JSON (for backup)
        attrib['verified'] = True
        attrib['verified_at'] = datetime.utcnow().isoformat()
        write_json_key(_attrib_key(uid), attrib)
        
        # Create or update DB attribution
        if attrib_db:
            attrib_db.verified = True
            attrib_db.verified_at = datetime.utcnow()
            logger.info(f"[affiliates.track.signup_verified] marked verified in DB for user={uid}")
        else:
            # Create DB record now
            try:
                ref = attrib.get('ref', '')
                db.add(AffiliateAttribution(
                    affiliate_uid=affiliate_uid,
                    user_uid=uid,
                    ref=ref,
                    verified=True,
                    verified_at=datetime.utcnow(),
                ))
                logger.info(f"[affiliates.track.signup_verified] created DB attribution for user={uid} affiliate={affiliate_uid}")
            except Exception as db_ex:
                logger.warning(f"[affiliates.track.signup_verified] failed to create DB attribution: {db_ex}")
        
        try:
            db.commit()
            logger.info(f"[affiliates.track.signup_verified] DB commit success for user={uid}")
        except Exception as commit_ex:
            db.rollback()
            logger.warning(f"[affiliates.track.signup_verified] DB commit failed: {commit_ex}")
        
        # DO NOT increment signups_total here - it causes double counting!
        # The stats endpoint counts verified attributions directly from the DB
        # This ensures accurate counts without race conditions
        
        return {"ok": True, "tracked": True, "affiliate_uid": affiliate_uid}
    except Exception as ex:
        logger.exception(f"[affiliates.track.signup_verified] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/policy")
async def affiliates_policy():
    """Public affiliate policy so frontend/backoffice can read canonical values."""
    return {
        "min_payout_cents": 10000,  # $100 minimum
        "currency": "usd",
        "schedule": "weekly",
        "payout_day": "friday",
        "rollover": True,
        "notes": "Minimum payout is $100. Remaining balances roll over to next cycle."
    }


@router.get("/profile")
async def affiliates_profile(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return {"profile": None}
    try:
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        if not prof:
            return {"profile": None}
        stats_json = read_json_key(_stats_key(uid)) or {}
        stats = {
            "clicks": int((stats_json.get("clicks") or 0)),
            "signups": int(prof.signups_total or 0),
            "conversions": int(prof.conversions_total or 0),
            "gross_cents": int(prof.gross_cents_total or 0),
            "payout_cents": int(prof.payout_cents_total or 0),
            "currency": "usd",
        }
        return {
            "uid": prof.uid,
            "role": "affiliate",
            "platform": prof.platform,
            "channel": prof.channel,
            "referralCode": prof.referral_code,
            "referralLink": prof.referral_link,
            "email": prof.email,
            "name": prof.name,
            "stats": stats,
        }
    except Exception as ex:
        logger.exception(f"[affiliates.profile] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)

@router.get("/stats")
async def affiliates_stats(request: Request, range: str = "all", db: Session = Depends(get_db)):
    """Return aggregated stats for the authenticated affiliate filtered by date range.
    
    Ranges:
    - 'today' or '1d': Last 24 hours
    - '7d' or 'week': Last 7 days  
    - '30d' or 'month': Last 30 days
    - '90d': Last 90 days
    - 'all': All-time totals from profile
    
    Data is fetched real-time from Neon PostgreSQL.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        from datetime import timedelta
        from sqlalchemy import or_
        
        # Always fetch fresh data from PostgreSQL - expire any cached state
        db.expire_all()
        
        # Get the affiliate profile
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        if not prof:
            logger.warning(f"[affiliates.stats] no profile found for uid={uid}")
            return {
                "clicks": 0,
                "signups": 0,
                "conversions": 0,
                "gross_cents": 0,
                "payout_cents": 0,
                "currency": "usd",
            }
        
        # Map range to days
        range_to_days = {
            'today': 1,
            '1d': 1,
            'week': 7,
            '7d': 7,
            'month': 30,
            '30d': 30,
            '90d': 90,
            'all': None
        }
        
        days = range_to_days.get(range, None)
        
        # For 'all' range, count directly from tables (source of truth)
        if days is None:
            # Count verified signups from attributions table
            signups_count = db.query(AffiliateAttribution).filter(
                AffiliateAttribution.affiliate_uid == uid,
                AffiliateAttribution.verified == True
            ).count()
            
            # Count conversions and sum amounts
            convs = db.query(AffiliateConversion).filter(AffiliateConversion.affiliate_uid == uid).all()
            conversions_count = len(convs)
            gross_cents = sum(int(c.amount_cents or 0) for c in convs)
            payout_cents = sum(int(c.payout_cents or 0) for c in convs)
            
            logger.info(f"[affiliates.stats] uid={uid} range=all clicks={prof.clicks_total} signups={signups_count} conversions={conversions_count}")
            return {
                "clicks": int(prof.clicks_total or 0),
                "signups": signups_count,
                "conversions": conversions_count,
                "gross_cents": gross_cents,
                "payout_cents": payout_cents,
                "currency": "usd",
                "last_click_at": prof.last_click_at.isoformat() if prof.last_click_at else None,
                "last_signup_at": prof.last_signup_at.isoformat() if prof.last_signup_at else None,
                "last_conversion_at": prof.last_conversion_at.isoformat() if prof.last_conversion_at else None,
            }
        
        # Calculate cutoff date for filtered ranges
        cutoff = datetime.utcnow() - timedelta(days=days)
        logger.info(f"[affiliates.stats] uid={uid} range={range} days={days} cutoff={cutoff.isoformat()}")
        
        # Count signups from affiliate_attributions in date range
        signups_count = 0
        last_signup_at = None
        attrs = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.affiliate_uid == uid)
            .filter(AffiliateAttribution.verified == True)
            .filter(
                or_(
                    AffiliateAttribution.verified_at >= cutoff,
                    (AffiliateAttribution.verified_at == None) & (AffiliateAttribution.attributed_at >= cutoff)
                )
            )
            .all()
        )
        for a in attrs:
            signups_count += 1
            ts = a.verified_at or a.attributed_at
            if ts and (not last_signup_at or ts > last_signup_at):
                last_signup_at = ts
        
        # Count conversions from affiliate_conversions in date range
        conversions_count = 0
        gross_cents = 0
        payout_cents = 0
        last_conversion_at = None
        convs = (
            db.query(AffiliateConversion)
            .filter(AffiliateConversion.affiliate_uid == uid)
            .filter(AffiliateConversion.created_at >= cutoff)
            .all()
        )
        for c in convs:
            conversions_count += 1
            gross_cents += int(c.amount_cents or 0)
            payout_cents += int(c.payout_cents or 0)
            ts = c.conversion_date or c.created_at
            if ts and (not last_conversion_at or ts > last_conversion_at):
                last_conversion_at = ts
        
        # Clicks - check if last_click_at is within range, otherwise use all-time
        # (we don't have per-click timestamps, only aggregate + last_click_at)
        clicks_count = 0
        if prof.last_click_at:
            # Handle timezone-aware vs naive datetime comparison
            last_click = prof.last_click_at
            if last_click.tzinfo is not None:
                last_click = last_click.replace(tzinfo=None)
            if last_click >= cutoff:
                # Some clicks happened in this period - show all-time clicks
                clicks_count = int(prof.clicks_total or 0)
        
        logger.info(f"[affiliates.stats] uid={uid} range={range} clicks={clicks_count} signups={signups_count} conversions={conversions_count}")
        
        return {
            "clicks": clicks_count,
            "signups": signups_count,
            "conversions": conversions_count,
            "gross_cents": gross_cents,
            "payout_cents": payout_cents,
            "currency": "usd",
            "last_signup_at": last_signup_at.isoformat() if last_signup_at else None,
            "last_conversion_at": last_conversion_at.isoformat() if last_conversion_at else None,
        }
    except Exception as ex:
        logger.exception(f"[affiliates.stats] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/stats/daily")
async def affiliates_stats_daily(request: Request, days: int = 30, db: Session = Depends(get_db)):
    """Return daily breakdown of affiliate stats for the last N days from Neon PostgreSQL."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # Expire cached data to get fresh results
        db.expire_all()
        
        days = max(1, min(days, 90))  # Limit between 1 and 90 days
        
        # Initialize daily buckets
        from datetime import timedelta
        from sqlalchemy import or_
        daily_stats = {}
        today = datetime.utcnow().date()
        
        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.isoformat()
            daily_stats[date_str] = {
                'date': date_str,
                'clicks': 0,
                'signups': 0,
                'conversions': 0,
                'gross_cents': 0
            }
        
        # Pull data from PostgreSQL (Neon)
        cutoff = datetime.utcnow() - timedelta(days=days)
        logger.info(f"[affiliates.stats.daily] uid={uid} days={days} cutoff={cutoff.isoformat()}")

        # Conversions from affiliate_conversions table
        convs = (
            db.query(AffiliateConversion)
            .filter(AffiliateConversion.affiliate_uid == uid)
            .filter(AffiliateConversion.created_at >= cutoff)
            .all()
        )
        logger.info(f"[affiliates.stats.daily] found {len(convs)} conversions")
        for c in convs:
            dt = (c.conversion_date or c.created_at).date()
            date_str = dt.isoformat()
            if date_str in daily_stats:
                daily_stats[date_str]['conversions'] += 1
                daily_stats[date_str]['gross_cents'] += int(c.amount_cents or 0)

        # Signups from affiliate_attributions table
        # Use verified_at for date filtering, fall back to attributed_at
        attrs = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.affiliate_uid == uid)
            .filter(AffiliateAttribution.verified == True)
            .filter(
                or_(
                    AffiliateAttribution.verified_at >= cutoff,
                    (AffiliateAttribution.verified_at == None) & (AffiliateAttribution.attributed_at >= cutoff)
                )
            )
            .all()
        )
        logger.info(f"[affiliates.stats.daily] found {len(attrs)} verified signups")
        for a in attrs:
            dt = (a.verified_at or a.attributed_at).date()
            date_str = dt.isoformat()
            if date_str in daily_stats:
                daily_stats[date_str]['signups'] += 1
        
        # Note: Clicks are not tracked per-day, only aggregate total
        # Return sorted by date (oldest first)
        result = sorted(daily_stats.values(), key=lambda x: x['date'])
        
        # Log summary
        total_signups = sum(d['signups'] for d in result)
        total_convs = sum(d['conversions'] for d in result)
        logger.info(f"[affiliates.stats.daily] returning {len(result)} days, total_signups={total_signups} total_conversions={total_convs}")
        
        return {"daily_stats": result}
        
    except Exception as ex:
        logger.exception(f"[affiliates.stats.daily] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/referrals")
async def affiliates_referrals(request: Request, db: Session = Depends(get_db)):
    """Return list of referrals for the authenticated affiliate from PostgreSQL (Neon)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # Expire cache to get fresh data from Neon
        db.expire_all()
        
        # Get all attributions for this affiliate
        attrs = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.affiliate_uid == uid)
            .order_by(AffiliateAttribution.attributed_at.desc())
            .limit(500)
            .all()
        )
        
        referrals = []
        for a in attrs:
            # Try to get user info
            user_info = db.query(User).filter(User.uid == a.user_uid).first()
            name = "User"
            plan = "free"
            billing_cycle = "yearly"
            
            if user_info:
                name = user_info.display_name or (user_info.email.split('@')[0] if user_info.email else "User")
                plan = user_info.plan or "free"
                billing_cycle = getattr(user_info, 'billing_cycle', 'yearly') or "yearly"
            
            # Check if there's a conversion for this user
            conv = db.query(AffiliateConversion).filter(
                AffiliateConversion.affiliate_uid == uid,
                AffiliateConversion.user_uid == a.user_uid
            ).first()
            
            status = "free"
            commission_cents = 0
            if conv:
                status = "paid"
                commission_cents = int(conv.payout_cents or 0)
            elif a.verified:
                status = "trial" if plan == "free" else "paid"
            
            referrals.append({
                "user_uid": a.user_uid,
                "name": name,
                "signup_date": (a.verified_at or a.attributed_at).isoformat() if (a.verified_at or a.attributed_at) else None,
                "status": status,
                "plan": plan,
                "billing_cycle": billing_cycle,
                "commission_cents": commission_cents,
                "verified": a.verified,
            })
        
        return {"referrals": referrals}
    except Exception as ex:
        logger.exception(f"[affiliates.referrals] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/payouts")
async def affiliates_payouts(request: Request, db: Session = Depends(get_db)):
    """Return payout summary and history for the authenticated affiliate from PostgreSQL (Neon)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # Expire cache to get fresh data from Neon
        db.expire_all()
        
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        if not prof:
            return {"pending_cents": 0, "paid_ytd_cents": 0, "history": []}
        
        # Calculate totals from conversions
        from datetime import datetime
        current_year = datetime.utcnow().year
        year_start = datetime(current_year, 1, 1)
        
        # Get all conversions for this affiliate
        all_convs = (
            db.query(AffiliateConversion)
            .filter(AffiliateConversion.affiliate_uid == uid)
            .all()
        )
        
        total_earned_cents = sum(int(c.payout_cents or 0) for c in all_convs)
        ytd_earned_cents = sum(
            int(c.payout_cents or 0) for c in all_convs 
            if c.created_at and c.created_at >= year_start
        )
        
        # For now, assume all earned is pending (no payout history table yet)
        # In production, you'd have a separate payouts table
        paid_out_cents = int(prof.payout_cents_total or 0) - total_earned_cents
        if paid_out_cents < 0:
            paid_out_cents = 0
        
        pending_cents = total_earned_cents - paid_out_cents
        if pending_cents < 0:
            pending_cents = 0
        
        # Build history from conversions (simplified - in production use a payouts table)
        history = []
        for c in sorted(all_convs, key=lambda x: x.created_at or datetime.min, reverse=True)[:50]:
            history.append({
                "date": (c.conversion_date or c.created_at).isoformat() if (c.conversion_date or c.created_at) else None,
                "amount_cents": int(c.payout_cents or 0),
                "status": "earned",
                "type": "commission",
            })
        
        return {
            "pending_cents": pending_cents,
            "paid_ytd_cents": ytd_earned_cents,
            "total_earned_cents": total_earned_cents,
            "currency": "usd",
            "history": history,
        }
    except Exception as ex:
        logger.exception(f"[affiliates.payouts] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/conversion")
async def affiliates_track_conversion(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Record a conversion for an affiliate. Called after successful payment.
    This is a backup method - primary conversion tracking happens in pricing_webhook.py
    """
    uid = get_uid_from_request(request)
    logger.info(f"[affiliates.track.conversion] uid={uid} payload={payload}")
    if not uid:
        logger.warning("[affiliates.track.conversion] no uid from request")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        amount_cents = int(payload.get("amount_cents") or 0)
        plan = str(payload.get("plan") or "").lower()
        billing_cycle = str(payload.get("billing_cycle") or "yearly").lower()
        
        # Look up attribution for this user - check DB first, then JSON fallback
        attrib_db = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == uid).first()
        affiliate_uid = attrib_db.affiliate_uid if attrib_db else None
        
        # Fallback to JSON attribution
        attrib_json = read_json_key(_attrib_key(uid)) or {}
        if not affiliate_uid:
            affiliate_uid = attrib_json.get('affiliate_uid')
        
        logger.info(f"[affiliates.track.conversion] attrib_db={attrib_db is not None} affiliate_uid={affiliate_uid} json_attrib={attrib_json}")
        
        if not affiliate_uid:
            logger.info(f"[affiliates.track.conversion] no attribution found for user={uid}")
            return {"ok": True, "tracked": False, "reason": "no_attribution"}
        
        # Check if conversion already exists for this user
        existing_conv = db.query(AffiliateConversion).filter(
            AffiliateConversion.affiliate_uid == affiliate_uid,
            AffiliateConversion.user_uid == uid
        ).first()
        
        if existing_conv:
            logger.info(f"[affiliates.track.conversion] conversion already exists for user={uid}")
            return {"ok": True, "tracked": False, "reason": "already_tracked"}
        
        # Calculate commission (30% default)
        commission_rate = float(os.getenv("AFFILIATE_COMMISSION_RATE", "0.30"))
        commission_cents = int(amount_cents * commission_rate)
        
        # Store conversion in JSON first (no FK constraints)
        try:
            conv_key = f"affiliates/conversions/{uid}.json"
            write_json_key(conv_key, {
                "affiliate_uid": affiliate_uid,
                "user_uid": uid,
                "amount_cents": amount_cents,
                "payout_cents": commission_cents,
                "plan": plan,
                "billing_cycle": billing_cycle,
                "converted_at": datetime.utcnow().isoformat(),
            })
            logger.info(f"[affiliates.track.conversion] stored in JSON for user={uid}")
        except Exception as json_ex:
            logger.warning(f"[affiliates.track.conversion] JSON write failed: {json_ex}")
        
        # Record conversion in DB
        try:
            db.add(AffiliateConversion(
                affiliate_uid=affiliate_uid,
                user_uid=uid,
                amount_cents=amount_cents,
                payout_cents=commission_cents,
                currency="usd",
                conversion_date=datetime.utcnow(),
            ))
            
            # Update affiliate profile aggregates
            prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == affiliate_uid).first()
            if prof:
                prof.conversions_total = int(prof.conversions_total or 0) + 1
                prof.gross_cents_total = int(prof.gross_cents_total or 0) + amount_cents
                prof.payout_cents_total = int(prof.payout_cents_total or 0) + commission_cents
                prof.last_conversion_at = datetime.utcnow()
                logger.info(f"[affiliates.track.conversion] updated profile for affiliate={affiliate_uid}")
            else:
                logger.warning(f"[affiliates.track.conversion] affiliate profile not found for uid={affiliate_uid}")
            
            db.commit()
            logger.info(f"[affiliates.track.conversion] DB success user={uid} affiliate={affiliate_uid} amount={amount_cents} commission={commission_cents}")
        except Exception as db_ex:
            db.rollback()
            logger.warning(f"[affiliates.track.conversion] DB insert failed: {db_ex}")
            # JSON storage is the fallback
        
        # Update JSON stats mirror
        try:
            stats = read_json_key(_stats_key(affiliate_uid)) or {}
            stats['conversions'] = int(stats.get('conversions') or 0) + 1
            stats['gross_cents'] = int(stats.get('gross_cents') or 0) + amount_cents
            stats['payout_cents'] = int(stats.get('payout_cents') or 0) + commission_cents
            stats['last_conversion_at'] = datetime.utcnow().isoformat()
            write_json_key(_stats_key(affiliate_uid), stats)
        except Exception as stats_ex:
            logger.warning(f"[affiliates.track.conversion] stats update failed: {stats_ex}")
        
        return {"ok": True, "tracked": True, "affiliate_uid": affiliate_uid, "commission_cents": commission_cents}
    except Exception as ex:
        logger.exception(f"[affiliates.track.conversion] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/debug/stats")
async def affiliates_debug_stats(request: Request, db: Session = Depends(get_db)):
    """Debug endpoint to verify affiliate stats are being tracked correctly."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        if not prof:
            return {"error": "not_an_affiliate"}
        
        # Count from tables directly
        attrs_count = db.query(AffiliateAttribution).filter(AffiliateAttribution.affiliate_uid == uid).count()
        verified_attrs_count = db.query(AffiliateAttribution).filter(
            AffiliateAttribution.affiliate_uid == uid,
            AffiliateAttribution.verified == True
        ).count()
        convs_count = db.query(AffiliateConversion).filter(AffiliateConversion.affiliate_uid == uid).count()
        
        # Sum from conversions
        convs = db.query(AffiliateConversion).filter(AffiliateConversion.affiliate_uid == uid).all()
        total_gross = sum(int(c.amount_cents or 0) for c in convs)
        total_payout = sum(int(c.payout_cents or 0) for c in convs)
        
        return {
            "profile": {
                "uid": prof.uid,
                "clicks_total": prof.clicks_total,
                "signups_total": prof.signups_total,
                "conversions_total": prof.conversions_total,
                "gross_cents_total": prof.gross_cents_total,
                "payout_cents_total": prof.payout_cents_total,
            },
            "calculated": {
                "attributions_count": attrs_count,
                "verified_signups_count": verified_attrs_count,
                "conversions_count": convs_count,
                "gross_cents_sum": total_gross,
                "payout_cents_sum": total_payout,
            },
            "match": {
                "signups": prof.signups_total == verified_attrs_count,
                "conversions": prof.conversions_total == convs_count,
                "gross": prof.gross_cents_total == total_gross,
                "payout": prof.payout_cents_total == total_payout,
            }
        }
    except Exception as ex:
        logger.exception(f"[affiliates.debug.stats] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)
