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
    if not ref or not new_user_uid:
        return JSONResponse({"error": "missing fields"}, status_code=400)
    affiliate_uid = _extract_affiliate_uid(ref)
    if not affiliate_uid:
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    try:
        write_json_key(_attrib_key(new_user_uid), {
            "affiliate_uid": affiliate_uid,
            "attributed_at": datetime.utcnow().isoformat(),
            "ref": ref,
            "verified": False,
        })
        # Persist in PostgreSQL (one attribution per user)
        existing = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == new_user_uid).first()
        if existing:
            existing.affiliate_uid = affiliate_uid
            existing.ref = ref
            existing.verified = False
            existing.attributed_at = datetime.utcnow()
            existing.verified_at = None
        else:
            db.add(AffiliateAttribution(
                affiliate_uid=affiliate_uid,
                user_uid=new_user_uid,
                ref=ref,
                verified=False,
            ))
        db.commit()
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.signup] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/signup_verified")
async def affiliates_track_signup_verified(request: Request, db: Session = Depends(get_db)):
    """After email verification, increment signup for the authenticated user if attributed."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        # Prefer DB attribution
        attrib_db = db.query(AffiliateAttribution).filter(AffiliateAttribution.user_uid == uid).first()
        affiliate_uid = attrib_db.affiliate_uid if attrib_db else None
        attrib = read_json_key(_attrib_key(uid)) or {}
        if not affiliate_uid:
            affiliate_uid = attrib.get('affiliate_uid')
        if not affiliate_uid:
            return {"ok": True}
        # Prevent double counting
        if attrib.get('verified'):
            return {"ok": True}
        attrib['verified'] = True
        attrib['verified_at'] = datetime.utcnow().isoformat()
        write_json_key(_attrib_key(uid), attrib)
        # Update DB attribution
        if attrib_db:
            attrib_db.verified = True
            attrib_db.verified_at = datetime.utcnow()
            db.commit()
        # Increment signup for affiliate
        # Update JSON stats mirror
        stats = read_json_key(_stats_key(affiliate_uid)) or {}
        stats['signups'] = int(stats.get('signups') or 0) + 1
        stats['last_signup_at'] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(affiliate_uid), stats)

        # Update PostgreSQL aggregates
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == affiliate_uid).first()
        if prof:
            prof.signups_total = int(prof.signups_total or 0) + 1
            prof.last_signup_at = datetime.utcnow()
            db.commit()
        return {"ok": True}
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

@router.get("/stats")
async def affiliates_stats(request: Request, range: str = "all", db: Session = Depends(get_db)):
    """Return aggregated stats for the authenticated affiliate, optionally filtered by date range."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        from datetime import timedelta
        
        # Map range values to days
        range_to_days = {
            '7d': 7,
            '30d': 30,
            '90d': 90,
            'all': None
        }
        
        days = range_to_days.get(range, None)
        
        # If 'all' or invalid range, return all-time aggregates from PostgreSQL + JSON mirror for clicks if needed
        if days is None:
            prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
            stats_json = read_json_key(_stats_key(uid)) or {}
            return {
                "clicks": int((prof.clicks_total if prof else 0) or stats_json.get("clicks") or 0),
                "signups": int((prof.signups_total if prof else 0) or 0),
                "conversions": int((prof.conversions_total if prof else 0) or 0),
                "gross_cents": int((prof.gross_cents_total if prof else 0) or 0),
                "payout_cents": int((prof.payout_cents_total if prof else 0) or 0),
                "currency": "usd",
                "last_click_at": prof.last_click_at.isoformat() if prof and prof.last_click_at else stats_json.get("last_click_at"),
                "last_signup_at": prof.last_signup_at.isoformat() if prof and prof.last_signup_at else None,
                "last_conversion_at": prof.last_conversion_at.isoformat() if prof and prof.last_conversion_at else None,
            }
        
        # For specific ranges, query PostgreSQL for date-filtered data
        
        # Calculate cutoff date
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        # Initialize stats
        signups_count = 0
        conversions_count = 0
        gross_cents = 0
        payout_cents = 0
        last_signup_at = None
        last_conversion_at = None
        
        # Get verified signups from affiliate_attributions
        attrs = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.affiliate_uid == uid)
            .filter(AffiliateAttribution.verified == True)
            .filter(AffiliateAttribution.attributed_at >= cutoff)
            .all()
        )
        for a in attrs:
            signups_count += 1
            ts = a.verified_at or a.attributed_at
            if ts and (not last_signup_at or ts > last_signup_at):
                last_signup_at = ts
        
        # Get conversions from affiliate_conversions
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
        
        # Clicks (no per-day timestamps stored) -> use all-time from profile/json
        prof = db.query(AffiliateProfile).filter(AffiliateProfile.uid == uid).first()
        clicks_count = int((prof.clicks_total if prof else 0) or 0)
        
        return {
            "clicks": clicks_count,  # All-time clicks (no date filtering available)
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
    """Return daily breakdown of affiliate stats for the last N days."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        days = max(1, min(days, 90))  # Limit between 1 and 90 days
        
        # Initialize daily buckets
        from datetime import datetime, timedelta
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
        
        # Pull data from PostgreSQL
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=days)

        # Conversions
        convs = (
            db.query(AffiliateConversion)
            .filter(AffiliateConversion.affiliate_uid == uid)
            .filter(AffiliateConversion.created_at >= cutoff)
            .all()
        )
        for c in convs:
            dt = (c.conversion_date or c.created_at).date()
            date_str = dt.isoformat()
            if date_str in daily_stats:
                daily_stats[date_str]['conversions'] += 1
                daily_stats[date_str]['gross_cents'] += int(c.amount_cents or 0)

        # Signups
        attrs = (
            db.query(AffiliateAttribution)
            .filter(AffiliateAttribution.affiliate_uid == uid)
            .filter(AffiliateAttribution.verified == True)
            .filter(AffiliateAttribution.attributed_at >= cutoff)
            .all()
        )
        for a in attrs:
            dt = (a.verified_at or a.attributed_at).date()
            date_str = dt.isoformat()
            if date_str in daily_stats:
                daily_stats[date_str]['signups'] += 1
        
        # Note: Clicks are not tracked daily in Firestore, so we can't provide historical click data
        # Return sorted by date (oldest first)
        result = sorted(daily_stats.values(), key=lambda x: x['date'])
        return {"daily_stats": result}
        
    except Exception as ex:
        logger.exception(f"[affiliates.stats.daily] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)
