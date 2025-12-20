"""
Booking System Router
Clean, modern booking/CRM system for photographers
"""
import secrets
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
            background_color=style.get("backgroundColor", "#ffffff"),
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
            "backgroundColor": form.background_color or "#ffffff",
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
                "backgroundColor": form.background_color or "#ffffff",
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
