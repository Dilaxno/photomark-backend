"""
Booking System Router
Clean, modern booking/CRM system for photographers
"""
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.booking import (
    Client, Booking, BookingPayment, SessionPackage, BookingSettings,
    BookingStatus, PaymentStatus, SessionType, BookingForm, FormSubmission
)
from utils.emailing import send_email_smtp, render_email

router = APIRouter(prefix="/api/booking", tags=["booking"])


# ============ Pydantic Models ============

class ClientCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = []
    source: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None


class BookingCreate(BaseModel):
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None
    title: Optional[str] = None
    session_type: Optional[str] = "other"
    session_date: Optional[str] = None
    duration_minutes: Optional[int] = 60
    location: Optional[str] = None
    status: Optional[str] = "inquiry"
    total_amount: Optional[float] = 0.0
    notes: Optional[str] = None


class BookingUpdate(BaseModel):
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None
    title: Optional[str] = None
    session_type: Optional[str] = None
    session_date: Optional[str] = None
    duration_minutes: Optional[int] = None
    location: Optional[str] = None
    status: Optional[str] = None
    total_amount: Optional[float] = None
    notes: Optional[str] = None


class FormCreate(BaseModel):
    name: str
    title: Optional[str] = None
    subtitle: Optional[str] = None
    fields: Optional[List[dict]] = []
    style: Optional[dict] = {}
    submit_button_text: Optional[str] = "Submit"
    success_message: Optional[str] = "Thank you for your submission!"
    redirect_url: Optional[str] = None
    is_published: Optional[bool] = False


class FormUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    fields: Optional[List[dict]] = None
    style: Optional[dict] = None
    submit_button_text: Optional[str] = None
    success_message: Optional[str] = None
    redirect_url: Optional[str] = None
    is_published: Optional[bool] = None


class SettingsUpdate(BaseModel):
    business_name: Optional[str] = None
    business_email: Optional[str] = None
    email_notifications: Optional[bool] = None


class SubmissionUpdate(BaseModel):
    status: Optional[str] = None


# ============ Helper Functions ============

def _send_booking_notification_email(
    owner_email: str,
    form_name: str,
    contact_name: Optional[str],
    contact_email: Optional[str],
    contact_phone: Optional[str],
    form_data: dict,
    form_fields: list,
    submission_id: str
):
    """Send email notification to form owner about new booking submission"""
    import os
    try:
        # Build form fields for email template
        email_fields = []
        field_labels = {f.get("id"): f.get("label", f.get("id")) for f in form_fields}
        
        # Skip contact fields we already show separately
        skip_types = {"name", "email", "phone"}
        skip_ids = set()
        for f in form_fields:
            if f.get("type") in skip_types:
                skip_ids.add(f.get("id"))
        
        for field_id, value in form_data.items():
            if field_id in skip_ids or not value:
                continue
            label = field_labels.get(field_id, field_id.replace("_", " ").title())
            # Format value
            if isinstance(value, list):
                display_value = ", ".join(str(v) for v in value)
            elif isinstance(value, str) and value.startswith("{"):
                # Try to parse JSON (e.g., location picker)
                try:
                    import json
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and "address" in parsed:
                        display_value = parsed["address"]
                    else:
                        display_value = str(value)
                except:
                    display_value = str(value)
            else:
                display_value = str(value)
            
            if len(display_value) > 200:
                display_value = display_value[:200] + "..."
            
            email_fields.append({"label": label, "value": display_value})
        
        # Get frontend URL for dashboard link
        frontend_url = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/")
        dashboard_url = f"{frontend_url}/booking" if frontend_url else "#"
        
        # Get contact initial for avatar
        contact_initial = (contact_name or "?")[0].upper()
        
        # Render email template
        html = render_email(
            "booking_notification.html",
            form_name=form_name,
            contact_name=contact_name or "Unknown",
            contact_email=contact_email,
            contact_phone=contact_phone,
            contact_initial=contact_initial,
            form_fields=email_fields if email_fields else None,
            dashboard_url=dashboard_url,
        )
        
        # Plain text version
        text = f"""New Booking Submission!

Someone submitted your booking form "{form_name}".

Contact Information:
- Name: {contact_name or 'Not provided'}
- Email: {contact_email or 'Not provided'}
- Phone: {contact_phone or 'Not provided'}

View the full submission in your dashboard: {dashboard_url}
"""
        
        # Send email
        success = send_email_smtp(
            to_addr=owner_email,
            subject=f"🎉 New Booking: {contact_name or 'New Lead'} via {form_name}",
            html=html,
            text=text,
            reply_to=contact_email if contact_email else None,
        )
        
        if success:
            logger.info(f"Booking notification email sent to {owner_email} for submission {submission_id}")
        else:
            logger.warning(f"Failed to send booking notification email to {owner_email}")
            
    except Exception as e:
        logger.error(f"Error sending booking notification email: {e}")


def _parse_session_type(value: str) -> SessionType:
    try:
        return SessionType(value.lower())
    except ValueError:
        return SessionType.OTHER


def _parse_booking_status(value: str) -> BookingStatus:
    try:
        return BookingStatus(value.lower())
    except ValueError:
        return BookingStatus.INQUIRY


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except:
        return None


def _generate_slug(name: str) -> str:
    """Generate URL-friendly slug from name"""
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_-]+', '-', slug)
    slug = slug.strip('-')
    return f"{slug}-{secrets.token_hex(4)}"



# ============ Dashboard ============

@router.get("/dashboard")
async def get_dashboard(request: Request):
    """Get booking dashboard with stats"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        total_clients = db.query(func.count(Client.id)).filter(Client.uid == uid).scalar() or 0
        total_bookings = db.query(func.count(Booking.id)).filter(Booking.uid == uid).scalar() or 0
        
        upcoming = db.query(Booking).filter(
            Booking.uid == uid,
            Booking.session_date >= now,
            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING])
        ).order_by(Booking.session_date.asc()).limit(5).all()
        
        recent = db.query(Booking).filter(Booking.uid == uid).order_by(
            Booking.created_at.desc()
        ).limit(10).all()
        
        month_revenue = db.query(func.sum(BookingPayment.amount)).filter(
            BookingPayment.uid == uid,
            BookingPayment.status == "completed",
            BookingPayment.paid_at >= month_start
        ).scalar() or 0.0
        
        return {
            "stats": {
                "total_clients": total_clients,
                "total_bookings": total_bookings,
                "month_revenue": month_revenue,
            },
            "upcoming": [b.to_dict() for b in upcoming],
            "recent": [b.to_dict() for b in recent],
        }
    finally:
        db.close()


# ============ Clients ============

@router.get("/clients")
async def list_clients(
    request: Request,
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """List all clients"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        query = db.query(Client).filter(Client.uid == uid)
        
        if search:
            term = f"%{search}%"
            query = query.filter(or_(
                Client.name.ilike(term),
                Client.email.ilike(term),
                Client.phone.ilike(term)
            ))
        
        total = query.count()
        clients = query.order_by(Client.created_at.desc()).offset(offset).limit(limit).all()
        
        return {"clients": [c.to_dict() for c in clients], "total": total}
    finally:
        db.close()


@router.post("/clients")
async def create_client(request: Request, data: ClientCreate):
    """Create a new client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        client = Client(
            uid=uid,
            name=data.name,
            email=data.email,
            phone=data.phone,
            company=data.company,
            notes=data.notes,
            tags=data.tags or [],
            source=data.source
        )
        db.add(client)
        db.commit()
        db.refresh(client)
        return client.to_dict()
    finally:
        db.close()


@router.get("/clients/{client_id}")
async def get_client(request: Request, client_id: str):
    """Get a specific client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id, Client.uid == uid).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        bookings = db.query(Booking).filter(Booking.client_id == client.id).order_by(Booking.session_date.desc()).all()
        
        result = client.to_dict()
        result["bookings"] = [b.to_dict() for b in bookings]
        return result
    finally:
        db.close()


@router.put("/clients/{client_id}")
async def update_client(request: Request, client_id: str, data: ClientUpdate):
    """Update a client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id, Client.uid == uid).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        for key, value in data.dict(exclude_unset=True).items():
            if value is not None:
                setattr(client, key, value)
        
        client.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(client)
        return client.to_dict()
    finally:
        db.close()


@router.delete("/clients/{client_id}")
async def delete_client(request: Request, client_id: str):
    """Delete a client"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        client = db.query(Client).filter(Client.id == client_id, Client.uid == uid).first()
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        db.delete(client)
        db.commit()
        return {"ok": True}
    finally:
        db.close()



# ============ Bookings ============

@router.get("/bookings")
async def list_bookings(
    request: Request,
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """List bookings with filters"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        query = db.query(Booking).filter(Booking.uid == uid)
        
        if status:
            query = query.filter(Booking.status == _parse_booking_status(status))
        
        if search:
            term = f"%{search}%"
            query = query.filter(or_(
                Booking.client_name.ilike(term),
                Booking.client_email.ilike(term),
                Booking.title.ilike(term)
            ))
        
        total = query.count()
        bookings = query.order_by(Booking.session_date.desc().nullslast(), Booking.created_at.desc()).offset(offset).limit(limit).all()
        
        return {"bookings": [b.to_dict() for b in bookings], "total": total}
    finally:
        db.close()


@router.post("/bookings")
async def create_booking(request: Request, data: BookingCreate):
    """Create a new booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        booking = Booking(
            uid=uid,
            client_id=data.client_id if data.client_id else None,
            client_name=data.client_name,
            client_email=data.client_email,
            client_phone=data.client_phone,
            title=data.title,
            session_type=_parse_session_type(data.session_type) if data.session_type else SessionType.OTHER,
            session_date=_parse_datetime(data.session_date) if data.session_date else None,
            duration_minutes=data.duration_minutes or 60,
            location=data.location,
            status=_parse_booking_status(data.status) if data.status else BookingStatus.INQUIRY,
            total_amount=data.total_amount or 0.0,
            notes=data.notes
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)
        return booking.to_dict()
    finally:
        db.close()


@router.get("/bookings/{booking_id}")
async def get_booking(request: Request, booking_id: str):
    """Get a specific booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        booking = db.query(Booking).filter(Booking.id == booking_id, Booking.uid == uid).first()
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        result = booking.to_dict()
        result["payments"] = [p.to_dict() for p in booking.payments]
        if booking.client:
            result["client"] = booking.client.to_dict()
        return result
    finally:
        db.close()


@router.put("/bookings/{booking_id}")
async def update_booking(request: Request, booking_id: str, data: BookingUpdate):
    """Update a booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        booking = db.query(Booking).filter(Booking.id == booking_id, Booking.uid == uid).first()
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                if key == "session_type":
                    booking.session_type = _parse_session_type(value)
                elif key == "status":
                    booking.status = _parse_booking_status(value)
                elif key == "session_date":
                    booking.session_date = _parse_datetime(value)
                else:
                    setattr(booking, key, value)
        
        booking.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(booking)
        return booking.to_dict()
    finally:
        db.close()


@router.delete("/bookings/{booking_id}")
async def delete_booking(request: Request, booking_id: str):
    """Delete a booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        booking = db.query(Booking).filter(Booking.id == booking_id, Booking.uid == uid).first()
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        db.delete(booking)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============ Calendar ============

@router.get("/calendar")
async def get_calendar(request: Request, start: str = Query(...), end: str = Query(...)):
    """Get bookings for calendar view"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    start_date = _parse_datetime(start)
    end_date = _parse_datetime(end)
    
    if not start_date or not end_date:
        return JSONResponse({"error": "Invalid date range"}, status_code=400)
    
    db: Session = next(get_db())
    try:
        bookings = db.query(Booking).filter(
            Booking.uid == uid,
            Booking.session_date >= start_date,
            Booking.session_date <= end_date
        ).order_by(Booking.session_date.asc()).all()
        
        events = []
        for b in bookings:
            events.append({
                "id": str(b.id),
                "title": b.title or b.client_name or "Booking",
                "client_name": b.client_name,
                "start": b.session_date.isoformat() if b.session_date else None,
                "end": b.session_end.isoformat() if b.session_end else None,
                "status": b.status.value if b.status else None,
                "session_type": b.session_type.value if b.session_type else None,
            })
        
        return {"events": events}
    finally:
        db.close()



# ============ Settings ============

@router.get("/settings")
async def get_settings(request: Request):
    """Get booking settings"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        settings = db.query(BookingSettings).filter(BookingSettings.uid == uid).first()
        if not settings:
            settings = BookingSettings(uid=uid)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        return settings.to_dict()
    finally:
        db.close()


@router.put("/settings")
async def update_settings(request: Request, data: SettingsUpdate):
    """Update booking settings"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        settings = db.query(BookingSettings).filter(BookingSettings.uid == uid).first()
        if not settings:
            settings = BookingSettings(uid=uid)
            db.add(settings)
        
        for key, value in data.dict(exclude_unset=True).items():
            if value is not None:
                setattr(settings, key, value)
        
        settings.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(settings)
        return settings.to_dict()
    finally:
        db.close()


# ============ Forms ============

@router.get("/forms")
async def list_forms(request: Request):
    """List all booking forms"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        forms = db.query(BookingForm).filter(BookingForm.uid == uid).order_by(BookingForm.created_at.desc()).all()
        return {"forms": [f.to_dict() for f in forms]}
    finally:
        db.close()


@router.post("/forms")
async def create_form(request: Request, data: FormCreate):
    """Create a new booking form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        # Generate unique slug
        slug = _generate_slug(data.name)
        
        # Check slug uniqueness
        existing = db.query(BookingForm).filter(BookingForm.slug == slug).first()
        if existing:
            slug = _generate_slug(data.name)
        
        # Extract style properties
        style = data.style or {}
        
        form = BookingForm(
            uid=uid,
            name=data.name,
            slug=slug,
            title=data.title,
            subtitle=data.subtitle,
            fields=data.fields or [],
            submit_button_text=data.submit_button_text or "Submit",
            success_message=data.success_message or "Thank you for your submission!",
            redirect_url=data.redirect_url,
            is_published=data.is_published or False,
            # Style properties
            font_family=style.get("fontFamily", "Inter"),
            primary_color=style.get("primaryColor", "#1f2937"),
            background_color=style.get("backgroundColor", "#f5f5f5"),
            form_bg_color=style.get("formBgColor", "#ffffff"),
            text_color=style.get("textColor", "#1f2937"),
            input_border_radius=style.get("borderRadius", 12),
            input_border_color=style.get("borderColor", "#e5e7eb"),
            input_bg_color=style.get("inputBgColor", "#f9fafb"),
        )
        db.add(form)
        db.commit()
        db.refresh(form)
        
        return _form_to_response(form)
    finally:
        db.close()


@router.get("/forms/{form_id}")
async def get_form(request: Request, form_id: str):
    """Get a specific form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        return _form_to_response(form)
    finally:
        db.close()


@router.put("/forms/{form_id}")
async def update_form(request: Request, form_id: str, data: FormUpdate):
    """Update a form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        
        # Handle style separately
        if "style" in update_data and update_data["style"]:
            style = update_data.pop("style")
            if "fontFamily" in style:
                form.font_family = style["fontFamily"]
            if "primaryColor" in style:
                form.primary_color = style["primaryColor"]
            if "backgroundColor" in style:
                form.background_color = style["backgroundColor"]
            if "formBgColor" in style:
                form.form_bg_color = style["formBgColor"]
            if "textColor" in style:
                form.text_color = style["textColor"]
            if "borderRadius" in style:
                form.input_border_radius = style["borderRadius"]
            if "borderColor" in style:
                form.input_border_color = style["borderColor"]
            if "inputBgColor" in style:
                form.input_bg_color = style["inputBgColor"]
        
        for key, value in update_data.items():
            if value is not None:
                setattr(form, key, value)
        
        form.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(form)
        
        return _form_to_response(form)
    finally:
        db.close()


@router.delete("/forms/{form_id}")
async def delete_form(request: Request, form_id: str):
    """Delete a form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        db.delete(form)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _form_to_response(form: BookingForm) -> dict:
    """Convert form to API response with style object"""
    return {
        "id": str(form.id),
        "name": form.name,
        "slug": form.slug,
        "title": form.title,
        "subtitle": form.subtitle,
        "fields": form.fields or [],
        "style": {
            "fontFamily": form.font_family or "Inter",
            "primaryColor": form.primary_color or "#1f2937",
            "backgroundColor": form.background_color or "#f5f5f5",
            "formBgColor": form.form_bg_color or "#ffffff",
            "textColor": form.text_color or "#1f2937",
            "borderRadius": form.input_border_radius or 12,
            "borderColor": form.input_border_color or "#e5e7eb",
            "inputBgColor": form.input_bg_color or "#f9fafb",
        },
        "submit_button_text": form.submit_button_text or "Submit",
        "success_message": form.success_message or "Thank you!",
        "redirect_url": form.redirect_url,
        "is_published": form.is_published,
        "views_count": form.views_count or 0,
        "submissions_count": form.submissions_count or 0,
        "created_at": form.created_at.isoformat() if form.created_at else None,
        "updated_at": form.updated_at.isoformat() if form.updated_at else None,
    }



# ============ Form Submissions ============

@router.get("/forms/{form_id}/submissions")
async def list_submissions(request: Request, form_id: str):
    """List submissions for a form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        submissions = db.query(FormSubmission).filter(
            FormSubmission.form_id == form_id
        ).order_by(FormSubmission.created_at.desc()).all()
        
        return {"submissions": [s.to_dict() for s in submissions]}
    finally:
        db.close()


@router.put("/forms/{form_id}/submissions/{submission_id}")
async def update_submission(request: Request, form_id: str, submission_id: str, data: SubmissionUpdate):
    """Update a submission status"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        submission = db.query(FormSubmission).filter(
            FormSubmission.id == submission_id,
            FormSubmission.form_id == form_id,
            FormSubmission.uid == uid
        ).first()
        
        if not submission:
            return JSONResponse({"error": "Submission not found"}, status_code=404)
        
        if data.status:
            submission.status = data.status
        
        db.commit()
        db.refresh(submission)
        return submission.to_dict()
    finally:
        db.close()


@router.delete("/forms/{form_id}/submissions/{submission_id}")
async def delete_submission(request: Request, form_id: str, submission_id: str):
    """Delete a submission"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        submission = db.query(FormSubmission).filter(
            FormSubmission.id == submission_id,
            FormSubmission.form_id == form_id,
            FormSubmission.uid == uid
        ).first()
        
        if not submission:
            return JSONResponse({"error": "Submission not found"}, status_code=404)
        
        # Update form submission count
        form = db.query(BookingForm).filter(BookingForm.id == form_id).first()
        if form and form.submissions_count > 0:
            form.submissions_count -= 1
        
        db.delete(submission)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============ Public Form Endpoints ============

@router.get("/public/form/{slug}")
async def get_public_form(slug: str):
    """Get a public form by slug (no auth required)"""
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # Increment view count
        form.views_count = (form.views_count or 0) + 1
        db.commit()
        
        # Get branding from settings
        settings = db.query(BookingSettings).filter(BookingSettings.uid == form.uid).first()
        branding = {}
        if settings:
            branding = {
                "logo": settings.brand_logo or settings.business_logo,
                "business_name": settings.business_name,
            }
        
        return {
            "id": str(form.id),
            "title": form.title,
            "subtitle": form.subtitle,
            "fields": form.fields or [],
            "style": {
                "fontFamily": form.font_family or "Inter",
                "primaryColor": form.primary_color or "#1f2937",
                "backgroundColor": form.background_color or "#f5f5f5",
                "formBgColor": form.form_bg_color or "#ffffff",
                "textColor": form.text_color or "#1f2937",
                "borderRadius": form.input_border_radius or 12,
                "borderColor": form.input_border_color or "#e5e7eb",
                "inputBgColor": form.input_bg_color or "#f9fafb",
            },
            "submit_button_text": form.submit_button_text or "Submit",
            "branding": branding,
        }
    finally:
        db.close()


@router.post("/public/form/{slug}/submit")
async def submit_public_form(request: Request, slug: str):
    """Submit a public form (no auth required)"""
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # Get submission data
        try:
            data = await request.json()
        except:
            return JSONResponse({"error": "Invalid data"}, status_code=400)
        
        # Extract contact info from data
        contact_name = None
        contact_email = None
        contact_phone = None
        
        for field in form.fields or []:
            field_id = field.get("id", "")
            field_type = field.get("type", "")
            value = data.get(field_id)
            
            if field_type == "name" and value:
                contact_name = str(value)
            elif field_type == "email" and value:
                contact_email = str(value)
            elif field_type == "phone" and value:
                contact_phone = str(value)
        
        # Get client info
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")
        
        # Create submission
        submission = FormSubmission(
            uid=form.uid,
            form_id=form.id,
            data=data,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            status="new",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(submission)
        
        # Update form submission count
        form.submissions_count = (form.submissions_count or 0) + 1
        
        db.commit()
        db.refresh(submission)
        
        # Send email notification to form owner (in background thread)
        # Get owner's notification settings
        settings = db.query(BookingSettings).filter(BookingSettings.uid == form.uid).first()
        owner_email = None
        should_send_email = True
        
        if settings:
            # Check if email notifications are enabled
            if settings.email_notifications is False:
                should_send_email = False
            # Use business email if set, otherwise we need to get user email from auth
            owner_email = settings.business_email
        
        # Also check form-specific notify_email
        if form.notify_email:
            owner_email = form.notify_email
        
        if should_send_email and owner_email:
            # Send email in background thread to not block response
            thread = threading.Thread(
                target=_send_booking_notification_email,
                args=(
                    owner_email,
                    form.name,
                    contact_name,
                    contact_email,
                    contact_phone,
                    data,
                    form.fields or [],
                    str(submission.id)
                )
            )
            thread.daemon = True
            thread.start()
        
        return {
            "ok": True,
            "success_message": form.success_message or "Thank you for your submission!",
            "redirect_url": form.redirect_url if form.redirect_url else None,
        }
    except Exception as e:
        logger.error(f"Form submission error: {e}")
        return JSONResponse({"error": "Failed to submit form"}, status_code=500)
    finally:
        db.close()


@router.get("/public/form/{slug}/calendar")
async def get_public_calendar(slug: str, start: str = Query(...), end: str = Query(...)):
    """Get calendar events for a public form (shows availability)"""
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        start_date = _parse_datetime(start)
        end_date = _parse_datetime(end)
        
        if not start_date or not end_date:
            return JSONResponse({"error": "Invalid date range"}, status_code=400)
        
        # Get bookings for this user in the date range
        bookings = db.query(Booking).filter(
            Booking.uid == form.uid,
            Booking.session_date >= start_date,
            Booking.session_date <= end_date,
            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING])
        ).all()
        
        events = []
        for b in bookings:
            events.append({
                "id": str(b.id),
                "title": "Booked",
                "client_name": b.client_name,
                "start": b.session_date.isoformat() if b.session_date else None,
                "end": b.session_end.isoformat() if b.session_end else None,
                "status": b.status.value if b.status else None,
                "session_type": b.session_type.value if b.session_type else None,
            })
        
        return {"events": events}
    finally:
        db.close()


# ============ File Upload for Forms ============

from fastapi import UploadFile, File

@router.post("/public/form/{slug}/upload")
async def upload_form_file(slug: str, file: UploadFile = File(...)):
    """Upload a file for a form submission (stored in form owner's R2 bucket)"""
    from utils.storage import upload_bytes, get_presigned_url
    import os
    
    db: Session = next(get_db())
    try:
        # Get the form to find the owner's uid
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        owner_uid = form.uid
        
        # Validate file
        if not file.filename:
            return JSONResponse({"error": "No file provided"}, status_code=400)
        
        # Check file size (max 10MB)
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            return JSONResponse({"error": "File too large (max 10MB)"}, status_code=400)
        
        # Get file extension
        _, ext = os.path.splitext(file.filename)
        ext = ext.lower()
        
        # Validate file type
        allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.doc', '.docx', '.txt', '.heic']
        if ext not in allowed_extensions:
            return JSONResponse({"error": f"File type not allowed. Allowed: {', '.join(allowed_extensions)}"}, status_code=400)
        
        # Generate unique filename
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        random_suffix = secrets.token_hex(4)
        safe_filename = "".join(c for c in file.filename if c.isalnum() or c in '._-')[:50]
        
        # Store in owner's booking_uploads folder
        key = f"users/{owner_uid}/booking_uploads/{form.id}/{timestamp}_{random_suffix}_{safe_filename}"
        
        # Determine content type
        content_type = file.content_type or "application/octet-stream"
        if ext in ['.jpg', '.jpeg']:
            content_type = "image/jpeg"
        elif ext == '.png':
            content_type = "image/png"
        elif ext == '.gif':
            content_type = "image/gif"
        elif ext == '.webp':
            content_type = "image/webp"
        elif ext == '.pdf':
            content_type = "application/pdf"
        elif ext in ['.doc', '.docx']:
            content_type = "application/msword"
        elif ext == '.txt':
            content_type = "text/plain"
        
        # Upload to R2
        url = upload_bytes(key, content, content_type=content_type, generate_thumbs=False)
        
        return {
            "ok": True,
            "key": key,
            "url": url,
            "filename": file.filename,
            "size": len(content),
            "content_type": content_type,
        }
    except Exception as e:
        logger.error(f"File upload error: {e}")
        return JSONResponse({"error": "Failed to upload file"}, status_code=500)
    finally:
        db.close()


@router.get("/forms/{form_id}/files")
async def list_form_files(request: Request, form_id: str):
    """List all files uploaded for a form's submissions"""
    from utils.storage import list_keys, get_presigned_url
    
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # List files in the form's upload folder
        prefix = f"users/{uid}/booking_uploads/{form_id}/"
        keys = list_keys(prefix, max_keys=500)
        
        files = []
        for key in keys:
            filename = key.split("/")[-1]
            # Remove timestamp prefix to get original filename
            parts = filename.split("_", 2)
            original_name = parts[2] if len(parts) > 2 else filename
            
            url = get_presigned_url(key, expires_in=3600)
            files.append({
                "key": key,
                "filename": original_name,
                "url": url,
            })
        
        return {"files": files}
    finally:
        db.close()


# ============ Notifications ============

@router.get("/notifications")
async def get_notifications(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False)
):
    """Get recent booking notifications (new submissions)"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        # Get recent submissions as notifications
        query = db.query(FormSubmission).filter(FormSubmission.uid == uid)
        
        if unread_only:
            query = query.filter(FormSubmission.status == "new")
        
        submissions = query.order_by(FormSubmission.created_at.desc()).limit(limit).all()
        
        # Get form names for context
        form_ids = list(set(str(s.form_id) for s in submissions))
        forms = db.query(BookingForm).filter(BookingForm.id.in_(form_ids)).all()
        form_map = {str(f.id): f.name for f in forms}
        
        notifications = []
        for sub in submissions:
            notifications.append({
                "id": str(sub.id),
                "type": "new_booking",
                "form_id": str(sub.form_id),
                "form_name": form_map.get(str(sub.form_id), "Unknown Form"),
                "contact_name": sub.contact_name or "Unknown",
                "contact_email": sub.contact_email,
                "status": sub.status,
                "is_read": sub.status != "new",
                "created_at": sub.created_at.isoformat() if sub.created_at else None,
            })
        
        # Count unread
        unread_count = db.query(func.count(FormSubmission.id)).filter(
            FormSubmission.uid == uid,
            FormSubmission.status == "new"
        ).scalar() or 0
        
        return {
            "notifications": notifications,
            "unread_count": unread_count,
        }
    finally:
        db.close()


@router.post("/notifications/{notification_id}/read")
async def mark_notification_read(request: Request, notification_id: str):
    """Mark a notification as read"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        submission = db.query(FormSubmission).filter(
            FormSubmission.id == notification_id,
            FormSubmission.uid == uid
        ).first()
        
        if not submission:
            return JSONResponse({"error": "Notification not found"}, status_code=404)
        
        if submission.status == "new":
            submission.status = "read"
            db.commit()
        
        return {"ok": True}
    finally:
        db.close()


@router.post("/notifications/read-all")
async def mark_all_notifications_read(request: Request):
    """Mark all notifications as read"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        db.query(FormSubmission).filter(
            FormSubmission.uid == uid,
            FormSubmission.status == "new"
        ).update({"status": "read"})
        db.commit()
        
        return {"ok": True}
    finally:
        db.close()

