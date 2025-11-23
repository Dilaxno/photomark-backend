from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
from datetime import datetime

from core.auth import get_uid_from_request
from core.config import logger
from utils.emailing import render_email, send_email_smtp
from utils.storage import read_json_key, write_json_key

# Firestore client via centralized helper
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore
from core.auth import get_fs_client as _get_fs_client

def _update_affiliate_profile_fs(affiliate_uid: str, stats: dict):
    """Mirror affiliate info (uid, referral link, stats) into users/<uid>.affiliate"""
    try:
        _fs = _get_fs_client()
        if not _fs or not affiliate_uid:
            return
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        
        # Fetch actual referral code from affiliate profile
        referral_code = affiliate_uid  # default to uid
        referral_link = f"{front}/?ref={affiliate_uid}"
        try:
            aff_doc = _fs.collection('affiliate_profiles').document(affiliate_uid).get()
            if aff_doc.exists:
                aff_data = aff_doc.to_dict()
                referral_code = aff_data.get('referralCode') or affiliate_uid
                referral_link = aff_data.get('referralLink') or f"{front}/?ref={referral_code}"
        except Exception:
            pass
        
        profile = {
            'affiliate': {
                'uid': affiliate_uid,
                'referralCode': referral_code,
                'referralLink': referral_link,
                'stats': {
                    'clicks': int(stats.get('clicks') or 0),
                    'signups': int(stats.get('signups') or 0),
                    'conversions': int(stats.get('conversions') or 0),
                    'gross_cents': int(stats.get('gross_cents') or 0),
                    'payout_cents': int(stats.get('payout_cents') or 0),
                    'currency': (stats.get('currency') or 'usd').lower(),
                    'last_click_at': stats.get('last_click_at'),
                    'last_signup_at': stats.get('last_signup_at'),
                    'last_conversion_at': stats.get('last_conversion_at'),
                },
                'updatedAt': datetime.utcnow(),
            }
        }
        _fs.collection('users').document(affiliate_uid).set(profile, merge=True)
    except Exception:
        pass

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
async def affiliates_register(request: Request, platform: str = Body(..., embed=True), channel: str = Body(..., embed=True)):
    """Finalize affiliate registration, persist profile, and send welcome email with referral link."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        _fs = _get_fs_client()
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")

        # Read existing profile (to avoid changing referral code if already set)
        existing = None
        if _fs is not None:
            doc_ref = _fs.collection('affiliate_profiles').document(uid)
            snap = doc_ref.get()
            existing = snap.to_dict() if snap.exists else None
        
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
        referral_code = (existing or {}).get('referralCode') or (f"{base}-{uid}" if base and len(base) >= 3 else uid)
        referral_link = (existing or {}).get('referralLink') or f"{front}/?ref={referral_code}"

        # Try to fetch user's email/name from Firestore users/{uid}
        email = None
        name = None
        try:
            if _fs is not None:
                udoc = _fs.collection('users').document(uid).get()
                if udoc.exists:
                    udata = udoc.to_dict()
                    email = udata.get('email')
                    name = udata.get('name') or udata.get('displayName')
        except Exception:
            pass

        # Persist affiliate profile
        if _fs is not None:
            data = {
                'uid': uid,
                'role': 'affiliate',
                'platform': platform,
                'channel': channel,
                'email': email,
                'name': name,
                'referralCode': referral_code,
                'referralLink': referral_link,
                'updatedAt': datetime.utcnow(),
            }
            if not existing:
                data['createdAt'] = datetime.utcnow()
            _fs.collection('affiliate_profiles').document(uid).set(data, merge=True)

        # Mirror minimal affiliate info under users/<uid>
        try:
            _update_affiliate_profile_fs(uid, read_json_key(_stats_key(uid)) or {})
        except Exception:
            pass

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
async def affiliates_track_click(payload: dict = Body(...)):
    """Record a click for a referral code. Public endpoint."""
    ref = str(payload.get("ref") or "").strip()
    uid = _extract_affiliate_uid(ref)
    if not uid:
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    try:
        stats = read_json_key(_stats_key(uid)) or {}
        stats["clicks"] = int(stats.get("clicks") or 0) + 1
        stats["last_click_at"] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(uid), stats)
        # Mirror in Firestore (lazy)
        try:
            _fs = _get_fs_client()
            if _fs:
                _fs.collection('affiliate_stats').document(uid).set({
                    **stats,
                    'uid': uid,
                    'updatedAt': datetime.utcnow(),
                }, merge=True)
                # Also mirror under user's document
                _update_affiliate_profile_fs(uid, stats)
        except Exception:
            pass
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.click] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/signup")
async def affiliates_track_signup(payload: dict = Body(...)):
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
        # Mirror attribution in Firestore for analytics if available (lazy)
        try:
            _fs = _get_fs_client()
            if _fs:
                _fs.collection('affiliate_attributions').document(new_user_uid).set({
                    'affiliate_uid': affiliate_uid,
                    'user_uid': new_user_uid,
                    'ref': ref,
                    'verified': False,
                    'attributed_at': datetime.utcnow(),
                }, merge=True)
        except Exception:
            pass
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.signup] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/signup_verified")
async def affiliates_track_signup_verified(request: Request):
    """After email verification, increment signup for the authenticated user if attributed."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        attrib = read_json_key(_attrib_key(uid)) or {}
        affiliate_uid = attrib.get('affiliate_uid')
        if not affiliate_uid:
            return {"ok": True}
        # Prevent double counting
        if attrib.get('verified'):
            return {"ok": True}
        attrib['verified'] = True
        attrib['verified_at'] = datetime.utcnow().isoformat()
        write_json_key(_attrib_key(uid), attrib)
        # Increment signup for affiliate
        stats = read_json_key(_stats_key(affiliate_uid)) or {}
        stats['signups'] = int(stats.get('signups') or 0) + 1
        stats['last_signup_at'] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(affiliate_uid), stats)
        # Mirror in Firestore (lazy)
        try:
            _fs = _get_fs_client()
            if _fs:
                _fs.collection('affiliate_stats').document(affiliate_uid).set({
                    **stats,
                    'uid': affiliate_uid,
                    'updatedAt': datetime.utcnow(),
                }, merge=True)
                _fs.collection('affiliate_attributions').document(uid).set({
                    **attrib,
                    'user_uid': uid,
                }, merge=True)

                # Append privacy-safe recent referral entry under affiliate_profiles/<affiliate_uid>
                try:
                    # Read user profile for name/plan
                    user_doc = _fs.collection('users').document(uid).get()
                    user_data = user_doc.to_dict() if user_doc.exists else {}
                    name = (user_data.get('name') or user_data.get('displayName') or user_data.get('email') or 'User').split('@')[0]
                    plan = (user_data.get('plan') or 'free')
                    status = 'paid' if str(plan).lower() in ('individual','studios','photographers','agencies','pro','team','enterprise','paid') else 'free'

                    prof_ref = _fs.collection('affiliate_profiles').document(affiliate_uid)
                    prof_snap = prof_ref.get()
                    prof = prof_snap.to_dict() if prof_snap.exists else {}
                    recents = list(prof.get('recent_referrals') or [])
                    recents.insert(0, {
                        'name': name,
                        'user_uid': uid,
                        'signup_date': datetime.utcnow(),
                        'status': status,
                        'plan': plan,
                    })
                    # cap to last 100
                    if len(recents) > 100:
                        recents = recents[:100]
                    prof_ref.set({ 'recent_referrals': recents, 'updatedAt': datetime.utcnow() }, merge=True)

                    # Notify affiliate via email about new signup (best-effort)
                    try:
                        aff_email = (prof.get('email') or None)
                        if aff_email:
                            app_name = os.getenv("APP_NAME", "Photomark")
                            front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                            subject = "New referral signup"
                            intro_html = (
                                f"Good news! A new user signed up via your referral link.<br><br>"
                                f"<b>User:</b> {name}<br>"
                                f"<b>Plan:</b> {plan}<br><br>"
                                f"View your dashboard: <a href=\"{front}/#affiliate-dashboard\">Affiliate Dashboard</a>"
                            )
                            html = render_email(
                                "email_basic.html",
                                title="New referral signup",
                                intro=intro_html,
                                button_label="Open Dashboard",
                                button_url=f"{front}/#affiliate-dashboard",
                            )
                            send_email_smtp(
                                aff_email,
                                subject,
                                html,
                                None,
                                from_addr=os.getenv("MAIL_FROM_AFFILIATES", "affiliates@photomark.cloud"),
                                reply_to=os.getenv("REPLY_TO_AFFILIATES", "affiliates@photomark.cloud"),
                                from_name=os.getenv("MAIL_FROM_NAME_AFFILIATES", "Photomark Partnerships"),
                            )
                    except Exception as _ex:
                        logger.warning(f"[affiliates.signup_verified] email notify failed: {_ex}")
                except Exception as _ex:
                    logger.warning(f"[affiliates.signup_verified] recent_referrals append failed: {_ex}")
        except Exception:
            pass
        # Also mirror affiliate profile under user's document
        try:
            _update_affiliate_profile_fs(affiliate_uid, stats)
        except Exception:
            pass
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
async def affiliates_stats(request: Request, range: str = "all"):
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
        
        # If 'all' or invalid range, return all-time stats from JSON
        if days is None:
            stats = read_json_key(_stats_key(uid)) or {}
            return {
                "clicks": int(stats.get("clicks") or 0),
                "signups": int(stats.get("signups") or 0),
                "conversions": int(stats.get("conversions") or 0),
                "gross_cents": int(stats.get("gross_cents") or 0),
                "payout_cents": int(stats.get("payout_cents") or 0),
                "currency": (stats.get("currency") or "usd").lower(),
                "last_click_at": stats.get("last_click_at"),
                "last_signup_at": stats.get("last_signup_at"),
                "last_conversion_at": stats.get("last_conversion_at"),
            }
        
        # For specific ranges, query Firestore for date-filtered data
        _fs = _get_fs_client()
        if not _fs:
            # Fallback to all-time stats if Firestore unavailable
            stats = read_json_key(_stats_key(uid)) or {}
            return {
                "clicks": int(stats.get("clicks") or 0),
                "signups": int(stats.get("signups") or 0),
                "conversions": int(stats.get("conversions") or 0),
                "gross_cents": int(stats.get("gross_cents") or 0),
                "payout_cents": int(stats.get("payout_cents") or 0),
                "currency": "usd",
            }
        
        # Calculate cutoff date
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        # Initialize stats
        signups_count = 0
        conversions_count = 0
        gross_cents = 0
        payout_cents = 0
        last_signup_at = None
        last_conversion_at = None
        
        # Get signups from affiliate_attributions
        try:
            attrs_ref = _fs.collection('affiliate_attributions')\
                .where('affiliate_uid', '==', uid)\
                .where('verified', '==', True)\
                .where('attributed_at', '>=', cutoff)\
                .stream()
            
            for doc in attrs_ref:
                signups_count += 1
                data = doc.to_dict()
                verified_at = data.get('verified_at') or data.get('attributed_at')
                if verified_at:
                    if hasattr(verified_at, 'seconds'):
                        verified_at = datetime.fromtimestamp(verified_at.seconds)
                    elif isinstance(verified_at, str):
                        verified_at = datetime.fromisoformat(verified_at.replace('Z', '+00:00'))
                    if not last_signup_at or verified_at > last_signup_at:
                        last_signup_at = verified_at
        except Exception as e:
            logger.warning(f"[affiliates.stats] failed to fetch signups: {e}")
        
        # Get conversions from affiliate_conversions
        try:
            convs_ref = _fs.collection('affiliate_conversions')\
                .where('affiliate_uid', '==', uid)\
                .where('createdAt', '>=', cutoff)\
                .stream()
            
            for doc in convs_ref:
                conversions_count += 1
                data = doc.to_dict()
                gross_cents += int(data.get('amount_cents') or 0)
                payout_cents += int(data.get('payout_cents') or 0)
                
                conv_date = data.get('conversion_date') or data.get('createdAt')
                if conv_date:
                    if hasattr(conv_date, 'seconds'):
                        conv_date = datetime.fromtimestamp(conv_date.seconds)
                    elif isinstance(conv_date, str):
                        conv_date = datetime.fromisoformat(conv_date.replace('Z', '+00:00'))
                    if not last_conversion_at or conv_date > last_conversion_at:
                        last_conversion_at = conv_date
        except Exception as e:
            logger.warning(f"[affiliates.stats] failed to fetch conversions: {e}")
        
        # Note: Clicks aren't stored with timestamps in Firestore, so we show all-time clicks
        all_time_stats = read_json_key(_stats_key(uid)) or {}
        clicks_count = int(all_time_stats.get("clicks") or 0)
        
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
async def affiliates_stats_daily(request: Request, days: int = 30):
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
        
        # Try to get data from Firestore if available
        _fs = _get_fs_client()
        if _fs:
            # Get conversions from affiliate_conversions collection
            try:
                cutoff = datetime.utcnow() - timedelta(days=days)
                conversions_ref = _fs.collection('affiliate_conversions')\
                    .where('affiliate_uid', '==', uid)\
                    .where('createdAt', '>=', cutoff)\
                    .stream()
                
                for doc in conversions_ref:
                    data = doc.to_dict()
                    conv_date = data.get('conversion_date') or data.get('createdAt')
                    if conv_date:
                        # Handle Firestore timestamp
                        if hasattr(conv_date, 'seconds'):
                            conv_date = datetime.fromtimestamp(conv_date.seconds)
                        elif isinstance(conv_date, str):
                            conv_date = datetime.fromisoformat(conv_date.replace('Z', '+00:00'))
                        
                        date_str = conv_date.date().isoformat()
                        if date_str in daily_stats:
                            daily_stats[date_str]['conversions'] += 1
                            daily_stats[date_str]['gross_cents'] += int(data.get('amount_cents') or 0)
            except Exception as e:
                logger.warning(f"[affiliates.stats.daily] failed to fetch conversions: {e}")
            
            # Get signups from affiliate_attributions collection
            try:
                cutoff = datetime.utcnow() - timedelta(days=days)
                attrs_ref = _fs.collection('affiliate_attributions')\
                    .where('affiliate_uid', '==', uid)\
                    .where('verified', '==', True)\
                    .where('attributed_at', '>=', cutoff)\
                    .stream()
                
                for doc in attrs_ref:
                    data = doc.to_dict()
                    attr_date = data.get('verified_at') or data.get('attributed_at')
                    if attr_date:
                        # Handle Firestore timestamp
                        if hasattr(attr_date, 'seconds'):
                            attr_date = datetime.fromtimestamp(attr_date.seconds)
                        elif isinstance(attr_date, str):
                            attr_date = datetime.fromisoformat(attr_date.replace('Z', '+00:00'))
                        
                        date_str = attr_date.date().isoformat()
                        if date_str in daily_stats:
                            daily_stats[date_str]['signups'] += 1
            except Exception as e:
                logger.warning(f"[affiliates.stats.daily] failed to fetch signups: {e}")
        
        # Note: Clicks are not tracked daily in Firestore, so we can't provide historical click data
        # Return sorted by date (oldest first)
        result = sorted(daily_stats.values(), key=lambda x: x['date'])
        return {"daily_stats": result}
        
    except Exception as ex:
        logger.exception(f"[affiliates.stats.daily] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)
