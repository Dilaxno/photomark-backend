"""
Abandoned Cart Recovery Router
Tracks cart sessions and sends recovery emails
"""
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Request, Query, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.abandoned_cart import AbandonedCart
from models.shop import Shop

router = APIRouter(prefix="/api/shop/cart", tags=["abandoned-cart"])


# ============ Pydantic Models ============

class CartItem(BaseModel):
    id: str
    title: str
    quantity: int
    price_cents: int
    currency: str = "USD"
    image_url: Optional[str] = None


class CartUpdate(BaseModel):
    session_id: str
    shop_slug: str
    items: List[CartItem]
    customer_email: Optional[str] = None
    customer_name: Optional[str] = None


class CartEmailCapture(BaseModel):
    session_id: str
    shop_slug: str
    email: str
    name: Optional[str] = None


# ============ Public Endpoints (Called from Shop Frontend) ============

@router.post("/track")
async def track_cart(
    request: Request,
    data: CartUpdate,
    db: Session = Depends(get_db)
):
    """Track cart activity - called when items are added/removed"""
    # Get shop
    shop = db.query(Shop).filter(Shop.slug == data.shop_slug).first()
    if not shop:
        return JSONResponse({"error": "Shop not found"}, status_code=404)
    
    # Calculate cart total
    cart_total = sum(item.price_cents * item.quantity for item in data.items)
    currency = data.items[0].currency if data.items else "USD"
    
    # Find or create cart session
    cart = db.query(AbandonedCart).filter(
        AbandonedCart.session_id == data.session_id,
        AbandonedCart.shop_uid == shop.uid,
        AbandonedCart.converted == False
    ).first()
    
    if cart:
        # Update existing cart
        cart.items = [item.dict() for item in data.items]
        cart.cart_total_cents = cart_total
        cart.currency = currency
        cart.last_activity_at = datetime.utcnow()
        if data.customer_email:
            cart.customer_email = data.customer_email.lower().strip()
        if data.customer_name:
            cart.customer_name = data.customer_name
    else:
        # Create new cart session
        cart = AbandonedCart(
            shop_uid=shop.uid,
            shop_slug=data.shop_slug,
            session_id=data.session_id,
            items=[item.dict() for item in data.items],
            cart_total_cents=cart_total,
            currency=currency,
            customer_email=data.customer_email.lower().strip() if data.customer_email else None,
            customer_name=data.customer_name,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            recovery_token=secrets.token_urlsafe(32)
        )
        db.add(cart)
    
    db.commit()
    
    return {"ok": True, "cart_id": str(cart.id)}


@router.post("/capture-email")
async def capture_email(
    request: Request,
    data: CartEmailCapture,
    db: Session = Depends(get_db)
):
    """Capture customer email during checkout - critical for recovery"""
    shop = db.query(Shop).filter(Shop.slug == data.shop_slug).first()
    if not shop:
        return JSONResponse({"error": "Shop not found"}, status_code=404)
    
    cart = db.query(AbandonedCart).filter(
        AbandonedCart.session_id == data.session_id,
        AbandonedCart.shop_uid == shop.uid,
        AbandonedCart.converted == False
    ).first()
    
    if cart:
        cart.customer_email = data.email.lower().strip()
        if data.name:
            cart.customer_name = data.name
        cart.last_activity_at = datetime.utcnow()
        db.commit()
    
    return {"ok": True}


@router.post("/convert")
async def mark_converted(
    session_id: str = Body(...),
    shop_slug: str = Body(...),
    payment_id: Optional[str] = Body(None),
    db: Session = Depends(get_db)
):
    """Mark cart as converted (purchase completed)"""
    shop = db.query(Shop).filter(Shop.slug == shop_slug).first()
    if not shop:
        return {"ok": True}  # Silently succeed even if shop not found
    
    cart = db.query(AbandonedCart).filter(
        AbandonedCart.session_id == session_id,
        AbandonedCart.shop_uid == shop.uid,
        AbandonedCart.converted == False
    ).first()
    
    if cart:
        cart.converted = True
        cart.converted_at = datetime.utcnow()
        cart.conversion_payment_id = payment_id
        db.commit()
    
    return {"ok": True}


@router.get("/recover/{token}")
async def recover_cart(
    token: str,
    db: Session = Depends(get_db)
):
    """Get cart contents from recovery link"""
    cart = db.query(AbandonedCart).filter(
        AbandonedCart.recovery_token == token,
        AbandonedCart.converted == False
    ).first()
    
    if not cart:
        return JSONResponse({"error": "Cart not found or already purchased"}, status_code=404)
    
    return {
        "shop_slug": cart.shop_slug,
        "items": cart.items,
        "cart_total_cents": cart.cart_total_cents,
        "currency": cart.currency,
        "customer_email": cart.customer_email,
        "customer_name": cart.customer_name
    }


# ============ Owner Endpoints (Dashboard) ============

@router.get("/abandoned")
async def list_abandoned_carts(
    request: Request,
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """List abandoned carts for shop owner"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    abandon_threshold = datetime.utcnow() - timedelta(hours=1)
    
    # Get abandoned carts (not converted, has email, inactive for 1+ hour)
    carts = db.query(AbandonedCart).filter(
        AbandonedCart.shop_uid == uid,
        AbandonedCart.converted == False,
        AbandonedCart.customer_email != None,
        AbandonedCart.last_activity_at < abandon_threshold,
        AbandonedCart.created_at > cutoff
    ).order_by(AbandonedCart.last_activity_at.desc()).limit(limit).all()
    
    # Stats
    total_abandoned = db.query(func.count(AbandonedCart.id)).filter(
        AbandonedCart.shop_uid == uid,
        AbandonedCart.converted == False,
        AbandonedCart.customer_email != None,
        AbandonedCart.last_activity_at < abandon_threshold,
        AbandonedCart.created_at > cutoff
    ).scalar() or 0
    
    total_value = db.query(func.sum(AbandonedCart.cart_total_cents)).filter(
        AbandonedCart.shop_uid == uid,
        AbandonedCart.converted == False,
        AbandonedCart.customer_email != None,
        AbandonedCart.last_activity_at < abandon_threshold,
        AbandonedCart.created_at > cutoff
    ).scalar() or 0
    
    recovered = db.query(func.count(AbandonedCart.id)).filter(
        AbandonedCart.shop_uid == uid,
        AbandonedCart.converted == True,
        AbandonedCart.recovery_email_sent == True,
        AbandonedCart.created_at > cutoff
    ).scalar() or 0
    
    return {
        "carts": [c.to_dict() for c in carts],
        "stats": {
            "total_abandoned": total_abandoned,
            "total_value_cents": total_value,
            "recovered_count": recovered
        }
    }


@router.post("/send-recovery/{cart_id}")
async def send_recovery_email(
    request: Request,
    cart_id: str,
    db: Session = Depends(get_db)
):
    """Manually send recovery email for a specific cart"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cart = db.query(AbandonedCart).filter(
        AbandonedCart.id == cart_id,
        AbandonedCart.shop_uid == uid
    ).first()
    
    if not cart:
        return JSONResponse({"error": "Cart not found"}, status_code=404)
    
    if not cart.customer_email:
        return JSONResponse({"error": "No email address for this cart"}, status_code=400)
    
    if cart.converted:
        return JSONResponse({"error": "Cart already converted"}, status_code=400)
    
    # Get shop for branding
    shop = db.query(Shop).filter(Shop.uid == uid).first()
    shop_name = shop.name if shop else "Our Shop"
    
    # Build recovery URL
    frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
    recovery_url = f"{frontend_origin}/shop/{cart.shop_slug}?recover={cart.recovery_token}"
    
    # Build email
    try:
        from utils.email import send_email_smtp
        from utils.email_templates import render_email
        
        # Format cart items for email
        items_html = ""
        for item in (cart.items or []):
            price = item.get('price_cents', 0) / 100
            items_html += f"<li>{item.get('title', 'Item')} x{item.get('quantity', 1)} - ${price:.2f}</li>"
        
        total = cart.cart_total_cents / 100
        
        html = render_email(
            "email_basic.html",
            title="You left something behind!",
            intro=f"""
            <p>Hi{' ' + cart.customer_name if cart.customer_name else ''},</p>
            <p>We noticed you didn't complete your purchase at <strong>{shop_name}</strong>. Your cart is still waiting for you:</p>
            <ul style="margin: 16px 0; padding-left: 20px;">{items_html}</ul>
            <p><strong>Total: ${total:.2f}</strong></p>
            <p>Complete your purchase now before these items are gone!</p>
            """,
            button_label="Complete My Purchase",
            button_url=recovery_url,
            footer_note="If you've already completed your purchase, please ignore this email."
        )
        
        sent = send_email_smtp(
            cart.customer_email,
            f"Complete your purchase at {shop_name}",
            html,
            f"Complete your purchase: {recovery_url}"
        )
        
        if sent:
            cart.recovery_email_sent = True
            cart.recovery_email_sent_at = datetime.utcnow()
            cart.recovery_email_count = (cart.recovery_email_count or 0) + 1
            db.commit()
            return {"ok": True, "message": "Recovery email sent"}
        else:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
            
    except Exception as e:
        logger.error(f"Failed to send recovery email: {e}")
        return JSONResponse({"error": "Failed to send email"}, status_code=500)


@router.post("/send-recovery-batch")
async def send_recovery_emails_batch(
    request: Request,
    max_emails: int = Body(10, ge=1, le=50),
    db: Session = Depends(get_db)
):
    """Send recovery emails to all eligible abandoned carts"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    abandon_threshold = datetime.utcnow() - timedelta(hours=1)
    max_age = datetime.utcnow() - timedelta(days=7)  # Don't email carts older than 7 days
    
    # Find eligible carts
    carts = db.query(AbandonedCart).filter(
        AbandonedCart.shop_uid == uid,
        AbandonedCart.converted == False,
        AbandonedCart.customer_email != None,
        AbandonedCart.recovery_email_count < 2,  # Max 2 emails per cart
        AbandonedCart.last_activity_at < abandon_threshold,
        AbandonedCart.created_at > max_age
    ).limit(max_emails).all()
    
    sent_count = 0
    for cart in carts:
        try:
            # Reuse the single send logic
            shop = db.query(Shop).filter(Shop.uid == uid).first()
            shop_name = shop.name if shop else "Our Shop"
            
            frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
            recovery_url = f"{frontend_origin}/shop/{cart.shop_slug}?recover={cart.recovery_token}"
            
            from utils.email import send_email_smtp
            from utils.email_templates import render_email
            
            items_html = ""
            for item in (cart.items or []):
                price = item.get('price_cents', 0) / 100
                items_html += f"<li>{item.get('title', 'Item')} x{item.get('quantity', 1)} - ${price:.2f}</li>"
            
            total = cart.cart_total_cents / 100
            
            html = render_email(
                "email_basic.html",
                title="You left something behind!",
                intro=f"""
                <p>Hi{' ' + cart.customer_name if cart.customer_name else ''},</p>
                <p>We noticed you didn't complete your purchase at <strong>{shop_name}</strong>. Your cart is still waiting:</p>
                <ul style="margin: 16px 0; padding-left: 20px;">{items_html}</ul>
                <p><strong>Total: ${total:.2f}</strong></p>
                """,
                button_label="Complete My Purchase",
                button_url=recovery_url,
            )
            
            sent = send_email_smtp(
                cart.customer_email,
                f"Complete your purchase at {shop_name}",
                html,
                f"Complete your purchase: {recovery_url}"
            )
            
            if sent:
                cart.recovery_email_sent = True
                cart.recovery_email_sent_at = datetime.utcnow()
                cart.recovery_email_count = (cart.recovery_email_count or 0) + 1
                sent_count += 1
                
        except Exception as e:
            logger.warning(f"Failed to send recovery email to {cart.customer_email}: {e}")
    
    db.commit()
    
    return {"ok": True, "sent_count": sent_count, "total_eligible": len(carts)}


# ============ Automatic Recovery (Called by Cron/Scheduler) ============

@router.post("/process-abandoned")
async def process_abandoned_carts(
    request: Request,
    api_key: str = Body(...),
    db: Session = Depends(get_db)
):
    """
    Process all abandoned carts and send recovery emails.
    Called by external cron job with API key for authentication.
    """
    expected_key = os.getenv("CRON_API_KEY", "")
    if not expected_key or api_key != expected_key:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    abandon_threshold = datetime.utcnow() - timedelta(hours=1)
    max_age = datetime.utcnow() - timedelta(days=7)
    
    # Find all eligible carts across all shops
    carts = db.query(AbandonedCart).filter(
        AbandonedCart.converted == False,
        AbandonedCart.customer_email != None,
        AbandonedCart.recovery_email_count < 2,
        AbandonedCart.last_activity_at < abandon_threshold,
        AbandonedCart.created_at > max_age
    ).limit(100).all()
    
    sent_count = 0
    errors = []
    
    for cart in carts:
        try:
            shop = db.query(Shop).filter(Shop.uid == cart.shop_uid).first()
            if not shop:
                continue
                
            shop_name = shop.name
            frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
            recovery_url = f"{frontend_origin}/shop/{cart.shop_slug}?recover={cart.recovery_token}"
            
            from utils.email import send_email_smtp
            from utils.email_templates import render_email
            
            items_html = ""
            for item in (cart.items or []):
                price = item.get('price_cents', 0) / 100
                items_html += f"<li>{item.get('title', 'Item')} x{item.get('quantity', 1)} - ${price:.2f}</li>"
            
            total = cart.cart_total_cents / 100
            
            html = render_email(
                "email_basic.html",
                title="You left something behind!",
                intro=f"""
                <p>Hi{' ' + cart.customer_name if cart.customer_name else ''},</p>
                <p>We noticed you didn't complete your purchase at <strong>{shop_name}</strong>.</p>
                <ul style="margin: 16px 0; padding-left: 20px;">{items_html}</ul>
                <p><strong>Total: ${total:.2f}</strong></p>
                """,
                button_label="Complete My Purchase",
                button_url=recovery_url,
            )
            
            sent = send_email_smtp(
                cart.customer_email,
                f"Complete your purchase at {shop_name}",
                html,
                f"Complete your purchase: {recovery_url}"
            )
            
            if sent:
                cart.recovery_email_sent = True
                cart.recovery_email_sent_at = datetime.utcnow()
                cart.recovery_email_count = (cart.recovery_email_count or 0) + 1
                sent_count += 1
                
        except Exception as e:
            errors.append(str(e))
            logger.warning(f"Failed to process abandoned cart {cart.id}: {e}")
    
    db.commit()
    
    return {
        "ok": True,
        "processed": len(carts),
        "sent": sent_count,
        "errors": len(errors)
    }
