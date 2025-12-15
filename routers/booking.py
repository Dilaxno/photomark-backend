"""
Booking System Router
Full booking/CRM system for photographers
"""
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Request, Query, HTTPException, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.booking import (
    Client, Booking, BookingPayment, SessionPackage, BookingSettings,
    BookingStatus, PaymentStatus, SessionType
)

router = APIRouter(prefix="/api/booking", tags=["booking"])


# ============ Pydantic Models ============

class ClientCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = []
    source: Optional[str] = None
    referral_source: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None
    referral_source: Optional[str] = None


class BookingCreate(BaseModel):
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None
    title: Optional[str] = None
    session_type: Optional[str] = "other"
    package_id: Optional[str] = None
    session_date: Optional[str] = None
    session_end: Optional[str] = None
    duration_minutes: Optional[int] = 60
    timezone: Optional[str] = "UTC"
    location: Optional[str] = None
    location_address: Optional[str] = None
    location_notes: Optional[str] = None
    is_virtual: Optional[bool] = False
    meeting_link: Optional[str] = None
    status: Optional[str] = "inquiry"
    total_amount: Optional[float] = 0.0
    deposit_amount: Optional[float] = 0.0
    currency: Optional[str] = "USD"
    notes: Optional[str] = None
    internal_notes: Optional[str] = None


class BookingUpdate(BaseModel):
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None
    title: Optional[str] = None
    session_type: Optional[str] = None
    package_id: Optional[str] = None
    session_date: Optional[str] = None
    session_end: Optional[str] = None
    duration_minutes: Optional[int] = None
    timezone: Optional[str] = None
    location: Optional[str] = None
    location_address: Optional[str] = None
    location_notes: Optional[str] = None
    is_virtual: Optional[bool] = None
    meeting_link: Optional[str] = None
    status: Optional[str] = None
    total_amount: Optional[float] = None
    deposit_amount: Optional[float] = None
    amount_paid: Optional[float] = None
    currency: Optional[str] = None
    payment_status: Optional[str] = None
    notes: Optional[str] = None
    internal_notes: Optional[str] = None
    contract_signed: Optional[bool] = None


class PackageCreate(BaseModel):
    name: str
    description: Optional[str] = None
    session_type: Optional[str] = "other"
    price: Optional[float] = 0.0
    currency: Optional[str] = "USD"
    deposit_amount: Optional[float] = 0.0
    deposit_percentage: Optional[float] = None
    duration_minutes: Optional[int] = 60
    included_photos: Optional[int] = None
    included_hours: Optional[float] = None
    deliverables: Optional[List[str]] = []
    is_active: Optional[bool] = True
    color: Optional[str] = None


class PaymentCreate(BaseModel):
    booking_id: str
    amount: float
    currency: Optional[str] = "USD"
    payment_type: Optional[str] = "payment"
    payment_method: Optional[str] = None
    notes: Optional[str] = None
    paid_at: Optional[str] = None


class SettingsUpdate(BaseModel):
    business_name: Optional[str] = None
    business_email: Optional[str] = None
    business_phone: Optional[str] = None
    availability: Optional[dict] = None
    default_duration: Optional[int] = None
    buffer_before: Optional[int] = None
    buffer_after: Optional[int] = None
    min_notice_hours: Optional[int] = None
    max_advance_days: Optional[int] = None
    default_currency: Optional[str] = None
    default_deposit_percentage: Optional[float] = None
    email_notifications: Optional[bool] = None
    sms_notifications: Optional[bool] = None
    booking_page_enabled: Optional[bool] = None
    booking_page_slug: Optional[str] = None
    booking_page_title: Optional[str] = None
    booking_page_description: Optional[str] = None
    timezone: Optional[str] = None


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


def _parse_payment_status(value: str) -> PaymentStatus:
    try:
        return PaymentStatus(value.lower())
    except ValueError:
        return PaymentStatus.UNPAID


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except:
        return None


# ============ Dashboard & Stats ============

@router.get("/dashboard")
async def get_dashboard(request: Request):
    """Get booking dashboard with stats and recent activity"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)
        
        # Stats
        total_clients = db.query(func.count(Client.id)).filter(Client.uid == uid).scalar() or 0
        total_bookings = db.query(func.count(Booking.id)).filter(Booking.uid == uid).scalar() or 0
        
        # Upcoming bookings (next 7 days)
        upcoming = db.query(Booking).filter(
            Booking.uid == uid,
            Booking.session_date >= now,
            Booking.session_date <= now + timedelta(days=7),
            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING])
        ).order_by(Booking.session_date.asc()).limit(5).all()
        
        # Recent bookings
        recent = db.query(Booking).filter(Booking.uid == uid).order_by(
            Booking.created_at.desc()
        ).limit(10).all()
        
        # Revenue this month
        month_revenue = db.query(func.sum(BookingPayment.amount)).filter(
            BookingPayment.uid == uid,
            BookingPayment.status == "completed",
            BookingPayment.paid_at >= month_start
        ).scalar() or 0.0
        
        # Bookings by status
        status_counts = {}
        for status in BookingStatus:
            count = db.query(func.count(Booking.id)).filter(
                Booking.uid == uid,
                Booking.status == status
            ).scalar() or 0
            status_counts[status.value] = count
        
        # Today's bookings
        today_bookings = db.query(Booking).filter(
            Booking.uid == uid,
            Booking.session_date >= today_start,
            Booking.session_date < today_start + timedelta(days=1)
        ).order_by(Booking.session_date.asc()).all()
        
        return {
            "stats": {
                "total_clients": total_clients,
                "total_bookings": total_bookings,
                "month_revenue": month_revenue,
                "status_counts": status_counts,
            },
            "upcoming": [b.to_dict() for b in upcoming],
            "recent": [b.to_dict() for b in recent],
            "today": [b.to_dict() for b in today_bookings],
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
            search_term = f"%{search}%"
            query = query.filter(or_(
                Client.name.ilike(search_term),
                Client.email.ilike(search_term),
                Client.phone.ilike(search_term),
                Client.company.ilike(search_term)
            ))
        
        total = query.count()
        clients = query.order_by(Client.created_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "clients": [c.to_dict() for c in clients],
            "total": total,
            "limit": limit,
            "offset": offset
        }
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
            address=data.address,
            city=data.city,
            state=data.state,
            zip_code=data.zip_code,
            country=data.country,
            notes=data.notes,
            tags=data.tags or [],
            source=data.source,
            referral_source=data.referral_source
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
        client = db.query(Client).filter(
            Client.id == client_id,
            Client.uid == uid
        ).first()
        
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        # Get client's bookings
        bookings = db.query(Booking).filter(
            Booking.client_id == client.id
        ).order_by(Booking.session_date.desc()).all()
        
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
        client = db.query(Client).filter(
            Client.id == client_id,
            Client.uid == uid
        ).first()
        
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
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
        client = db.query(Client).filter(
            Client.id == client_id,
            Client.uid == uid
        ).first()
        
        if not client:
            return JSONResponse({"error": "Client not found"}, status_code=404)
        
        db.delete(client)
        db.commit()
        return {"ok": True, "message": "Client deleted"}
    finally:
        db.close()


# ============ Bookings ============

@router.get("/bookings")
async def list_bookings(
    request: Request,
    status: Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
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
        
        if client_id:
            query = query.filter(Booking.client_id == client_id)
        
        if start_date:
            start = _parse_datetime(start_date)
            if start:
                query = query.filter(Booking.session_date >= start)
        
        if end_date:
            end = _parse_datetime(end_date)
            if end:
                query = query.filter(Booking.session_date <= end)
        
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                Booking.client_name.ilike(search_term),
                Booking.client_email.ilike(search_term),
                Booking.title.ilike(search_term),
                Booking.location.ilike(search_term)
            ))
        
        total = query.count()
        bookings = query.order_by(Booking.session_date.desc().nullslast(), Booking.created_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "bookings": [b.to_dict() for b in bookings],
            "total": total,
            "limit": limit,
            "offset": offset
        }
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
            package_id=data.package_id if data.package_id else None,
            session_date=_parse_datetime(data.session_date) if data.session_date else None,
            session_end=_parse_datetime(data.session_end) if data.session_end else None,
            duration_minutes=data.duration_minutes or 60,
            timezone=data.timezone or "UTC",
            location=data.location,
            location_address=data.location_address,
            location_notes=data.location_notes,
            is_virtual=data.is_virtual or False,
            meeting_link=data.meeting_link,
            status=_parse_booking_status(data.status) if data.status else BookingStatus.INQUIRY,
            total_amount=data.total_amount or 0.0,
            deposit_amount=data.deposit_amount or 0.0,
            currency=data.currency or "USD",
            notes=data.notes,
            internal_notes=data.internal_notes
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)
        return booking.to_dict()
    finally:
        db.close()


@router.get("/bookings/{booking_id}")
async def get_booking(request: Request, booking_id: str):
    """Get a specific booking with payments"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        booking = db.query(Booking).filter(
            Booking.id == booking_id,
            Booking.uid == uid
        ).first()
        
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        result = booking.to_dict()
        result["payments"] = [p.to_dict() for p in booking.payments]
        
        # Include client details if linked
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
        booking = db.query(Booking).filter(
            Booking.id == booking_id,
            Booking.uid == uid
        ).first()
        
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        
        for key, value in update_data.items():
            if value is not None:
                if key == "session_type":
                    booking.session_type = _parse_session_type(value)
                elif key == "status":
                    booking.status = _parse_booking_status(value)
                elif key == "payment_status":
                    booking.payment_status = _parse_payment_status(value)
                elif key in ["session_date", "session_end"]:
                    setattr(booking, key, _parse_datetime(value))
                elif key == "contract_signed" and value:
                    booking.contract_signed = True
                    booking.contract_signed_at = datetime.utcnow()
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
        booking = db.query(Booking).filter(
            Booking.id == booking_id,
            Booking.uid == uid
        ).first()
        
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        db.delete(booking)
        db.commit()
        return {"ok": True, "message": "Booking deleted"}
    finally:
        db.close()


# ============ Packages ============

@router.get("/packages")
async def list_packages(request: Request, active_only: bool = Query(False)):
    """List session packages"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        query = db.query(SessionPackage).filter(SessionPackage.uid == uid)
        if active_only:
            query = query.filter(SessionPackage.is_active == True)
        
        packages = query.order_by(SessionPackage.price.asc()).all()
        return {"packages": [p.to_dict() for p in packages]}
    finally:
        db.close()


@router.post("/packages")
async def create_package(request: Request, data: PackageCreate):
    """Create a session package"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        package = SessionPackage(
            uid=uid,
            name=data.name,
            description=data.description,
            session_type=_parse_session_type(data.session_type) if data.session_type else SessionType.OTHER,
            price=data.price or 0.0,
            currency=data.currency or "USD",
            deposit_amount=data.deposit_amount or 0.0,
            deposit_percentage=data.deposit_percentage,
            duration_minutes=data.duration_minutes or 60,
            included_photos=data.included_photos,
            included_hours=data.included_hours,
            deliverables=data.deliverables or [],
            is_active=data.is_active if data.is_active is not None else True,
            color=data.color
        )
        db.add(package)
        db.commit()
        db.refresh(package)
        return package.to_dict()
    finally:
        db.close()


@router.put("/packages/{package_id}")
async def update_package(request: Request, package_id: str, data: PackageCreate):
    """Update a session package"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        package = db.query(SessionPackage).filter(
            SessionPackage.id == package_id,
            SessionPackage.uid == uid
        ).first()
        
        if not package:
            return JSONResponse({"error": "Package not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                if key == "session_type":
                    package.session_type = _parse_session_type(value)
                else:
                    setattr(package, key, value)
        
        package.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(package)
        return package.to_dict()
    finally:
        db.close()


@router.delete("/packages/{package_id}")
async def delete_package(request: Request, package_id: str):
    """Delete a session package"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        package = db.query(SessionPackage).filter(
            SessionPackage.id == package_id,
            SessionPackage.uid == uid
        ).first()
        
        if not package:
            return JSONResponse({"error": "Package not found"}, status_code=404)
        
        db.delete(package)
        db.commit()
        return {"ok": True, "message": "Package deleted"}
    finally:
        db.close()


# ============ Payments ============

@router.post("/payments")
async def create_payment(request: Request, data: PaymentCreate):
    """Record a payment for a booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        # Verify booking exists and belongs to user
        booking = db.query(Booking).filter(
            Booking.id == data.booking_id,
            Booking.uid == uid
        ).first()
        
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        
        payment = BookingPayment(
            uid=uid,
            booking_id=booking.id,
            amount=data.amount,
            currency=data.currency or "USD",
            payment_type=data.payment_type or "payment",
            payment_method=data.payment_method,
            notes=data.notes,
            paid_at=_parse_datetime(data.paid_at) if data.paid_at else datetime.utcnow(),
            status="completed"
        )
        db.add(payment)
        
        # Update booking payment totals
        booking.amount_paid = (booking.amount_paid or 0) + data.amount
        if booking.amount_paid >= booking.total_amount:
            booking.payment_status = PaymentStatus.PAID
        elif booking.amount_paid > 0:
            booking.payment_status = PaymentStatus.PARTIAL
        
        db.commit()
        db.refresh(payment)
        return payment.to_dict()
    finally:
        db.close()


@router.get("/payments")
async def list_payments(
    request: Request,
    booking_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """List payments"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        query = db.query(BookingPayment).filter(BookingPayment.uid == uid)
        
        if booking_id:
            query = query.filter(BookingPayment.booking_id == booking_id)
        
        total = query.count()
        payments = query.order_by(BookingPayment.paid_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "payments": [p.to_dict() for p in payments],
            "total": total
        }
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
            # Create default settings
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
        
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(settings, key, value)
        
        settings.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(settings)
        return settings.to_dict()
    finally:
        db.close()


# ============ Calendar View ============

@router.get("/calendar")
async def get_calendar(
    request: Request,
    start: str = Query(...),
    end: str = Query(...)
):
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
        
        # Format for calendar
        events = []
        for b in bookings:
            events.append({
                "id": str(b.id),
                "title": b.title or b.client_name or "Booking",
                "start": b.session_date.isoformat() if b.session_date else None,
                "end": b.session_end.isoformat() if b.session_end else None,
                "status": b.status.value if b.status else None,
                "client_name": b.client_name,
                "location": b.location,
                "session_type": b.session_type.value if b.session_type else None,
            })
        
        return {"events": events}
    finally:
        db.close()


# ============ Form Builder ============

from models.booking import BookingForm, FormSubmission

class FormCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    fields: Optional[List[dict]] = []
    style: Optional[dict] = {}
    submit_button_text: Optional[str] = "Submit"
    success_message: Optional[str] = "Thank you for your submission!"
    redirect_url: Optional[str] = None
    notify_email: Optional[str] = None
    send_confirmation: Optional[bool] = True


class FormUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    fields: Optional[List[dict]] = None
    style: Optional[dict] = None
    submit_button_text: Optional[str] = None
    success_message: Optional[str] = None
    redirect_url: Optional[str] = None
    notify_email: Optional[str] = None
    send_confirmation: Optional[bool] = None
    is_active: Optional[bool] = None
    is_published: Optional[bool] = None


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
        # Generate slug if not provided
        slug = data.slug
        if not slug:
            import re
            slug = re.sub(r'[^a-z0-9]+', '-', data.name.lower()).strip('-')
        
        # Ensure slug is unique for this user
        existing = db.query(BookingForm).filter(BookingForm.uid == uid, BookingForm.slug == slug).first()
        if existing:
            slug = f"{slug}-{secrets.token_hex(4)}"
        
        form = BookingForm(
            uid=uid,
            name=data.name,
            slug=slug,
            description=data.description,
            title=data.title or data.name,
            subtitle=data.subtitle,
            fields=data.fields or [],
            style=data.style or {},
            submit_button_text=data.submit_button_text or "Submit",
            success_message=data.success_message or "Thank you for your submission!",
            redirect_url=data.redirect_url,
            notify_email=data.notify_email,
            send_confirmation=data.send_confirmation if data.send_confirmation is not None else True
        )
        db.add(form)
        db.commit()
        db.refresh(form)
        return form.to_dict()
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
        return form.to_dict()
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
        for key, value in update_data.items():
            if value is not None:
                setattr(form, key, value)
        
        form.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(form)
        return form.to_dict()
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
        return {"ok": True, "message": "Form deleted"}
    finally:
        db.close()


# ============ Public Form Endpoints (for embed) ============

@router.get("/public/form/{slug}")
async def get_public_form(slug: str):
    """Get a published form by slug (public, no auth required)"""
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True,
            BookingForm.is_active == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # Increment view count
        form.views_count = (form.views_count or 0) + 1
        db.commit()
        
        # Return only public-safe data
        return {
            "id": str(form.id),
            "title": form.title,
            "subtitle": form.subtitle,
            "fields": form.fields or [],
            "style": form.style or {},
            "submit_button_text": form.submit_button_text,
        }
    finally:
        db.close()


@router.post("/public/form/{slug}/submit")
async def submit_public_form(request: Request, slug: str, data: dict = Body(...)):
    """Submit a form (public, no auth required)"""
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(
            BookingForm.slug == slug,
            BookingForm.is_published == True,
            BookingForm.is_active == True
        ).first()
        
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # Extract contact info from submission
        contact_name = None
        contact_email = None
        contact_phone = None
        scheduled_date = None
        scheduled_end = None
        
        for field in form.fields or []:
            field_id = field.get("id")
            field_type = field.get("type")
            value = data.get(field_id)
            
            if field_type in ["name", "full_name"] and value:
                contact_name = str(value)
            elif field_type == "email" and value:
                contact_email = str(value)
            elif field_type == "phone" and value:
                contact_phone = str(value)
            elif field_type == "calendar" and value:
                scheduled_date = _parse_datetime(value.get("start") if isinstance(value, dict) else value)
                if isinstance(value, dict) and value.get("end"):
                    scheduled_end = _parse_datetime(value.get("end"))
        
        # Get request metadata
        ip_address = request.headers.get("x-forwarded-for", request.client.host if request.client else None)
        user_agent = request.headers.get("user-agent")
        referrer = request.headers.get("referer")
        
        submission = FormSubmission(
            uid=form.uid,
            form_id=form.id,
            data=data,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            scheduled_date=scheduled_date,
            scheduled_end=scheduled_end,
            ip_address=ip_address,
            user_agent=user_agent,
            referrer=referrer
        )
        db.add(submission)
        
        # Update form stats
        form.submissions_count = (form.submissions_count or 0) + 1
        
        db.commit()
        db.refresh(submission)
        
        return {
            "ok": True,
            "submission_id": str(submission.id),
            "success_message": form.success_message,
            "redirect_url": form.redirect_url
        }
    finally:
        db.close()


# ============ Form Submissions ============

@router.get("/forms/{form_id}/submissions")
async def list_form_submissions(
    request: Request,
    form_id: str,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """List submissions for a form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        # Verify form ownership
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        query = db.query(FormSubmission).filter(FormSubmission.form_id == form_id)
        
        if status:
            query = query.filter(FormSubmission.status == status)
        
        total = query.count()
        submissions = query.order_by(FormSubmission.created_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "submissions": [s.to_dict() for s in submissions],
            "total": total,
            "form": form.to_dict()
        }
    finally:
        db.close()


@router.put("/submissions/{submission_id}/status")
async def update_submission_status(request: Request, submission_id: str, status: str = Body(..., embed=True)):
    """Update submission status"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        submission = db.query(FormSubmission).filter(
            FormSubmission.id == submission_id,
            FormSubmission.uid == uid
        ).first()
        
        if not submission:
            return JSONResponse({"error": "Submission not found"}, status_code=404)
        
        submission.status = status
        db.commit()
        return {"ok": True, "status": status}
    finally:
        db.close()


@router.post("/submissions/{submission_id}/convert")
async def convert_submission_to_booking(request: Request, submission_id: str):
    """Convert a form submission to a booking"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        submission = db.query(FormSubmission).filter(
            FormSubmission.id == submission_id,
            FormSubmission.uid == uid
        ).first()
        
        if not submission:
            return JSONResponse({"error": "Submission not found"}, status_code=404)
        
        # Create booking from submission
        booking = Booking(
            uid=uid,
            client_name=submission.contact_name,
            client_email=submission.contact_email,
            client_phone=submission.contact_phone,
            session_date=submission.scheduled_date,
            session_end=submission.scheduled_end,
            status=BookingStatus.INQUIRY,
            questionnaire_data=submission.data
        )
        db.add(booking)
        db.flush()
        
        # Link submission to booking
        submission.booking_id = booking.id
        submission.status = "converted"
        
        db.commit()
        db.refresh(booking)
        
        return {"ok": True, "booking": booking.to_dict()}
    finally:
        db.close()


# ============ Form Analytics ============

@router.get("/forms/{form_id}/analytics")
async def get_form_analytics(request: Request, form_id: str):
    """Get analytics for a form"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        form = db.query(BookingForm).filter(BookingForm.id == form_id, BookingForm.uid == uid).first()
        if not form:
            return JSONResponse({"error": "Form not found"}, status_code=404)
        
        # Get submission stats
        total_submissions = db.query(func.count(FormSubmission.id)).filter(FormSubmission.form_id == form_id).scalar() or 0
        
        # Submissions by status
        status_counts = {}
        for status in ["new", "read", "contacted", "converted", "archived"]:
            count = db.query(func.count(FormSubmission.id)).filter(
                FormSubmission.form_id == form_id,
                FormSubmission.status == status
            ).scalar() or 0
            status_counts[status] = count
        
        # Conversion rate
        conversion_rate = (status_counts.get("converted", 0) / total_submissions * 100) if total_submissions > 0 else 0
        
        # Submissions over time (last 30 days)
        from datetime import timedelta
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        daily_submissions = db.query(
            func.date(FormSubmission.created_at).label('date'),
            func.count(FormSubmission.id).label('count')
        ).filter(
            FormSubmission.form_id == form_id,
            FormSubmission.created_at >= thirty_days_ago
        ).group_by(func.date(FormSubmission.created_at)).all()
        
        return {
            "views": form.views_count or 0,
            "submissions": total_submissions,
            "conversion_rate": round(conversion_rate, 1),
            "status_counts": status_counts,
            "daily_submissions": [{"date": str(d.date), "count": d.count} for d in daily_submissions]
        }
    finally:
        db.close()


# ============ Mini Sessions (UseSession.com style) ============

from models.booking import MiniSession, MiniSessionDate, MiniSessionSlot, Waitlist


class MiniSessionCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    session_type: Optional[str] = "portrait"
    duration_minutes: Optional[int] = 20
    buffer_minutes: Optional[int] = 10
    price: Optional[float] = 0.0
    deposit_amount: Optional[float] = 0.0
    currency: Optional[str] = "USD"
    included_photos: Optional[int] = None
    deliverables: Optional[List[str]] = []
    location_name: Optional[str] = None
    location_address: Optional[str] = None
    location_notes: Optional[str] = None
    cover_image: Optional[str] = None
    gallery_images: Optional[List[str]] = []
    max_bookings_per_slot: Optional[int] = 1
    allow_waitlist: Optional[bool] = True
    require_deposit: Optional[bool] = True
    auto_confirm: Optional[bool] = False


class MiniSessionDateCreate(BaseModel):
    session_date: str  # ISO date string
    location_name: Optional[str] = None
    location_address: Optional[str] = None
    notes: Optional[str] = None
    time_slots: Optional[List[str]] = []  # List of start times like ["09:00", "09:30", "10:00"]


class SlotBookingRequest(BaseModel):
    slot_id: str
    client_name: str
    client_email: str
    client_phone: Optional[str] = None
    notes: Optional[str] = None
    participants: Optional[int] = 1  # Number of people in session


class WaitlistRequest(BaseModel):
    mini_session_id: str
    session_date_id: Optional[str] = None
    name: str
    email: str
    phone: Optional[str] = None
    preferred_dates: Optional[List[str]] = []
    preferred_times: Optional[List[str]] = []
    notes: Optional[str] = None


@router.get("/mini-sessions")
async def list_mini_sessions(request: Request, active_only: bool = Query(False)):
    """List all mini-sessions for the photographer"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        query = db.query(MiniSession).filter(MiniSession.uid == uid)
        if active_only:
            query = query.filter(MiniSession.is_active == True)
        
        sessions = query.order_by(MiniSession.created_at.desc()).all()
        
        # Get booking counts for each session
        result = []
        for session in sessions:
            data = session.to_dict()
            # Count total slots and booked slots
            total_slots = db.query(func.count(MiniSessionSlot.id)).join(MiniSessionDate).filter(
                MiniSessionDate.mini_session_id == session.id
            ).scalar() or 0
            booked_slots = db.query(func.count(MiniSessionSlot.id)).join(MiniSessionDate).filter(
                MiniSessionDate.mini_session_id == session.id,
                MiniSessionSlot.status == "booked"
            ).scalar() or 0
            data["total_slots"] = total_slots
            data["booked_slots"] = booked_slots
            data["available_slots"] = total_slots - booked_slots
            result.append(data)
        
        return {"mini_sessions": result}
    finally:
        db.close()


@router.post("/mini-sessions")
async def create_mini_session(request: Request, data: MiniSessionCreate):
    """Create a new mini-session event"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        # Generate slug
        slug = data.slug
        if not slug:
            import re
            slug = re.sub(r'[^a-z0-9]+', '-', data.name.lower()).strip('-')
        
        # Ensure unique slug
        existing = db.query(MiniSession).filter(MiniSession.uid == uid, MiniSession.slug == slug).first()
        if existing:
            slug = f"{slug}-{secrets.token_hex(4)}"
        
        session = MiniSession(
            uid=uid,
            name=data.name,
            slug=slug,
            description=data.description,
            session_type=_parse_session_type(data.session_type) if data.session_type else SessionType.PORTRAIT,
            duration_minutes=data.duration_minutes or 20,
            buffer_minutes=data.buffer_minutes or 10,
            price=data.price or 0.0,
            deposit_amount=data.deposit_amount or 0.0,
            currency=data.currency or "USD",
            included_photos=data.included_photos,
            deliverables=data.deliverables or [],
            location_name=data.location_name,
            location_address=data.location_address,
            location_notes=data.location_notes,
            cover_image=data.cover_image,
            gallery_images=data.gallery_images or [],
            max_bookings_per_slot=data.max_bookings_per_slot or 1,
            allow_waitlist=data.allow_waitlist if data.allow_waitlist is not None else True,
            require_deposit=data.require_deposit if data.require_deposit is not None else True,
            auto_confirm=data.auto_confirm or False
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.to_dict()
    finally:
        db.close()


@router.get("/mini-sessions/{session_id}")
async def get_mini_session(request: Request, session_id: str):
    """Get a mini-session with all dates and slots"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        result = session.to_dict()
        result["dates"] = [d.to_dict() for d in session.dates]
        
        # Get waitlist count
        waitlist_count = db.query(func.count(Waitlist.id)).filter(
            Waitlist.mini_session_id == session.id,
            Waitlist.status == "waiting"
        ).scalar() or 0
        result["waitlist_count"] = waitlist_count
        
        return result
    finally:
        db.close()


@router.put("/mini-sessions/{session_id}")
async def update_mini_session(request: Request, session_id: str, data: MiniSessionCreate):
    """Update a mini-session"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                if key == "session_type":
                    session.session_type = _parse_session_type(value)
                else:
                    setattr(session, key, value)
        
        session.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(session)
        return session.to_dict()
    finally:
        db.close()


@router.delete("/mini-sessions/{session_id}")
async def delete_mini_session(request: Request, session_id: str):
    """Delete a mini-session"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        db.delete(session)
        db.commit()
        return {"ok": True, "message": "Mini-session deleted"}
    finally:
        db.close()


@router.post("/mini-sessions/{session_id}/publish")
async def publish_mini_session(request: Request, session_id: str):
    """Publish a mini-session to make it bookable"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        # Validate has at least one date with slots
        has_slots = db.query(MiniSessionSlot).join(MiniSessionDate).filter(
            MiniSessionDate.mini_session_id == session.id
        ).first()
        
        if not has_slots:
            return JSONResponse({"error": "Add at least one date with time slots before publishing"}, status_code=400)
        
        session.is_published = True
        session.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "slug": session.slug}
    finally:
        db.close()


# ============ Mini Session Dates & Slots ============

@router.post("/mini-sessions/{session_id}/dates")
async def add_mini_session_date(request: Request, session_id: str, data: MiniSessionDateCreate):
    """Add a date with time slots to a mini-session"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        # Parse the date
        session_date = _parse_datetime(data.session_date)
        if not session_date:
            return JSONResponse({"error": "Invalid date format"}, status_code=400)
        
        # Create the date record
        date_record = MiniSessionDate(
            uid=uid,
            mini_session_id=session.id,
            session_date=session_date,
            location_name=data.location_name or session.location_name,
            location_address=data.location_address or session.location_address,
            notes=data.notes
        )
        db.add(date_record)
        db.flush()
        
        # Create time slots
        if data.time_slots:
            for time_str in data.time_slots:
                try:
                    # Parse time like "09:00" or "14:30"
                    hour, minute = map(int, time_str.split(':'))
                    start_time = session_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    end_time = start_time + timedelta(minutes=session.duration_minutes)
                    
                    slot = MiniSessionSlot(
                        uid=uid,
                        session_date_id=date_record.id,
                        start_time=start_time,
                        end_time=end_time,
                        status="available"
                    )
                    db.add(slot)
                except:
                    continue  # Skip invalid time formats
        
        db.commit()
        db.refresh(date_record)
        return date_record.to_dict()
    finally:
        db.close()


@router.post("/mini-sessions/{session_id}/dates/{date_id}/generate-slots")
async def generate_time_slots(
    request: Request,
    session_id: str,
    date_id: str,
    start_time: str = Query(...),  # "09:00"
    end_time: str = Query(...)     # "17:00"
):
    """Auto-generate time slots for a date based on duration and buffer"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        date_record = db.query(MiniSessionDate).filter(
            MiniSessionDate.id == date_id,
            MiniSessionDate.mini_session_id == session.id
        ).first()
        
        if not date_record:
            return JSONResponse({"error": "Date not found"}, status_code=404)
        
        # Parse start and end times
        try:
            start_hour, start_min = map(int, start_time.split(':'))
            end_hour, end_min = map(int, end_time.split(':'))
        except:
            return JSONResponse({"error": "Invalid time format. Use HH:MM"}, status_code=400)
        
        # Generate slots
        slot_duration = session.duration_minutes + session.buffer_minutes
        current_time = date_record.session_date.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
        end_datetime = date_record.session_date.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)
        
        slots_created = 0
        while current_time + timedelta(minutes=session.duration_minutes) <= end_datetime:
            slot = MiniSessionSlot(
                uid=uid,
                session_date_id=date_record.id,
                start_time=current_time,
                end_time=current_time + timedelta(minutes=session.duration_minutes),
                status="available"
            )
            db.add(slot)
            slots_created += 1
            current_time += timedelta(minutes=slot_duration)
        
        db.commit()
        return {"ok": True, "slots_created": slots_created}
    finally:
        db.close()


@router.delete("/mini-sessions/dates/{date_id}")
async def delete_mini_session_date(request: Request, date_id: str):
    """Delete a mini-session date and all its slots"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        date_record = db.query(MiniSessionDate).filter(
            MiniSessionDate.id == date_id,
            MiniSessionDate.uid == uid
        ).first()
        
        if not date_record:
            return JSONResponse({"error": "Date not found"}, status_code=404)
        
        # Check for booked slots
        booked = db.query(MiniSessionSlot).filter(
            MiniSessionSlot.session_date_id == date_id,
            MiniSessionSlot.status == "booked"
        ).first()
        
        if booked:
            return JSONResponse({"error": "Cannot delete date with booked slots"}, status_code=400)
        
        db.delete(date_record)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.delete("/mini-sessions/slots/{slot_id}")
async def delete_slot(request: Request, slot_id: str):
    """Delete a single time slot"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        slot = db.query(MiniSessionSlot).filter(
            MiniSessionSlot.id == slot_id,
            MiniSessionSlot.uid == uid
        ).first()
        
        if not slot:
            return JSONResponse({"error": "Slot not found"}, status_code=404)
        
        if slot.status == "booked":
            return JSONResponse({"error": "Cannot delete booked slot"}, status_code=400)
        
        db.delete(slot)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.put("/mini-sessions/slots/{slot_id}/block")
async def block_slot(request: Request, slot_id: str):
    """Block a slot (make unavailable)"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        slot = db.query(MiniSessionSlot).filter(
            MiniSessionSlot.id == slot_id,
            MiniSessionSlot.uid == uid
        ).first()
        
        if not slot:
            return JSONResponse({"error": "Slot not found"}, status_code=404)
        
        if slot.status == "booked":
            return JSONResponse({"error": "Cannot block booked slot"}, status_code=400)
        
        slot.status = "blocked"
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.put("/mini-sessions/slots/{slot_id}/unblock")
async def unblock_slot(request: Request, slot_id: str):
    """Unblock a slot"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        slot = db.query(MiniSessionSlot).filter(
            MiniSessionSlot.id == slot_id,
            MiniSessionSlot.uid == uid
        ).first()
        
        if not slot:
            return JSONResponse({"error": "Slot not found"}, status_code=404)
        
        slot.status = "available"
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ============ Public Mini Session Booking ============

@router.get("/public/mini-session/{slug}")
async def get_public_mini_session(slug: str):
    """Get a published mini-session for public booking page"""
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.slug == slug,
            MiniSession.is_published == True,
            MiniSession.is_active == True
        ).first()
        
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        
        # Increment view count
        session.views_count = (session.views_count or 0) + 1
        db.commit()
        
        # Get available dates with slots
        dates = db.query(MiniSessionDate).filter(
            MiniSessionDate.mini_session_id == session.id,
            MiniSessionDate.is_active == True,
            MiniSessionDate.session_date >= datetime.utcnow()
        ).order_by(MiniSessionDate.session_date.asc()).all()
        
        dates_data = []
        for date in dates:
            available_slots = db.query(MiniSessionSlot).filter(
                MiniSessionSlot.session_date_id == date.id,
                MiniSessionSlot.status == "available",
                or_(
                    MiniSessionSlot.held_until == None,
                    MiniSessionSlot.held_until < datetime.utcnow()
                )
            ).order_by(MiniSessionSlot.start_time.asc()).all()
            
            if available_slots:
                dates_data.append({
                    "id": str(date.id),
                    "date": date.session_date.isoformat(),
                    "location_name": date.location_name or session.location_name,
                    "location_address": date.location_address or session.location_address,
                    "slots": [s.to_dict() for s in available_slots]
                })
        
        # Get photographer settings for branding
        settings = db.query(BookingSettings).filter(BookingSettings.uid == session.uid).first()
        
        return {
            "id": str(session.id),
            "name": session.name,
            "description": session.description,
            "session_type": session.session_type.value if session.session_type else None,
            "duration_minutes": session.duration_minutes,
            "price": session.price,
            "deposit_amount": session.deposit_amount,
            "currency": session.currency,
            "included_photos": session.included_photos,
            "deliverables": session.deliverables or [],
            "location_name": session.location_name,
            "location_address": session.location_address,
            "cover_image": session.cover_image,
            "gallery_images": session.gallery_images or [],
            "allow_waitlist": session.allow_waitlist,
            "require_deposit": session.require_deposit,
            "dates": dates_data,
            "photographer": {
                "name": settings.business_name if settings else None,
                "logo": settings.business_logo if settings else None,
                "accent_color": settings.booking_page_accent_color if settings else "#6366f1",
            } if settings else None
        }
    finally:
        db.close()


@router.post("/public/mini-session/{slug}/hold")
async def hold_slot(slug: str, slot_id: str = Body(..., embed=True), email: str = Body(..., embed=True)):
    """Temporarily hold a slot during checkout (10 min hold)"""
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.slug == slug,
            MiniSession.is_published == True
        ).first()
        
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        
        slot = db.query(MiniSessionSlot).join(MiniSessionDate).filter(
            MiniSessionSlot.id == slot_id,
            MiniSessionDate.mini_session_id == session.id
        ).first()
        
        if not slot:
            return JSONResponse({"error": "Slot not found"}, status_code=404)
        
        # Check if available
        if slot.status != "available":
            return JSONResponse({"error": "Slot is no longer available"}, status_code=400)
        
        # Check if held by someone else
        if slot.held_until and slot.held_until > datetime.utcnow() and slot.held_by_email != email:
            return JSONResponse({"error": "Slot is being held by another customer"}, status_code=400)
        
        # Hold for 10 minutes
        slot.status = "held"
        slot.held_until = datetime.utcnow() + timedelta(minutes=10)
        slot.held_by_email = email
        db.commit()
        
        return {"ok": True, "held_until": slot.held_until.isoformat()}
    finally:
        db.close()


@router.post("/public/mini-session/{slug}/book")
async def book_mini_session_slot(slug: str, data: SlotBookingRequest):
    """Book a mini-session slot"""
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.slug == slug,
            MiniSession.is_published == True
        ).first()
        
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        
        slot = db.query(MiniSessionSlot).join(MiniSessionDate).filter(
            MiniSessionSlot.id == data.slot_id,
            MiniSessionDate.mini_session_id == session.id
        ).first()
        
        if not slot:
            return JSONResponse({"error": "Slot not found"}, status_code=404)
        
        # Verify slot is available or held by this email
        if slot.status == "booked":
            return JSONResponse({"error": "Slot is already booked"}, status_code=400)
        
        if slot.status == "held" and slot.held_by_email != data.client_email:
            if slot.held_until and slot.held_until > datetime.utcnow():
                return JSONResponse({"error": "Slot is being held by another customer"}, status_code=400)
        
        # Get the date record
        date_record = db.query(MiniSessionDate).filter(MiniSessionDate.id == slot.session_date_id).first()
        
        # Create booking
        booking = Booking(
            uid=session.uid,
            client_name=data.client_name,
            client_email=data.client_email,
            client_phone=data.client_phone,
            title=session.name,
            session_type=session.session_type,
            session_date=slot.start_time,
            session_end=slot.end_time,
            duration_minutes=session.duration_minutes,
            location=date_record.location_name or session.location_name,
            location_address=date_record.location_address or session.location_address,
            status=BookingStatus.CONFIRMED if session.auto_confirm else BookingStatus.PENDING,
            total_amount=session.price,
            deposit_amount=session.deposit_amount,
            currency=session.currency,
            notes=data.notes,
            questionnaire_data={"participants": data.participants}
        )
        db.add(booking)
        db.flush()
        
        # Update slot
        slot.status = "booked"
        slot.booking_id = booking.id
        slot.held_until = None
        slot.held_by_email = None
        
        db.commit()
        db.refresh(booking)
        
        return {
            "ok": True,
            "booking_id": str(booking.id),
            "status": booking.status.value,
            "confirmation_message": f"Your {session.name} session is {'confirmed' if session.auto_confirm else 'pending confirmation'}!",
            "session_date": slot.start_time.isoformat(),
            "location": date_record.location_name or session.location_name
        }
    finally:
        db.close()


# ============ Waitlist ============

@router.post("/public/mini-session/{slug}/waitlist")
async def join_waitlist(slug: str, data: WaitlistRequest):
    """Join the waitlist for a mini-session"""
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.slug == slug,
            MiniSession.is_published == True,
            MiniSession.allow_waitlist == True
        ).first()
        
        if not session:
            return JSONResponse({"error": "Session not found or waitlist not available"}, status_code=404)
        
        # Check if already on waitlist
        existing = db.query(Waitlist).filter(
            Waitlist.mini_session_id == session.id,
            Waitlist.email == data.email,
            Waitlist.status == "waiting"
        ).first()
        
        if existing:
            return JSONResponse({"error": "You're already on the waitlist"}, status_code=400)
        
        waitlist_entry = Waitlist(
            uid=session.uid,
            mini_session_id=session.id,
            session_date_id=data.session_date_id if data.session_date_id else None,
            name=data.name,
            email=data.email,
            phone=data.phone,
            preferred_dates=data.preferred_dates or [],
            preferred_times=data.preferred_times or [],
            notes=data.notes
        )
        db.add(waitlist_entry)
        db.commit()
        
        return {"ok": True, "message": "You've been added to the waitlist. We'll notify you when a spot opens up!"}
    finally:
        db.close()


@router.get("/mini-sessions/{session_id}/waitlist")
async def get_waitlist(request: Request, session_id: str):
    """Get waitlist for a mini-session"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        session = db.query(MiniSession).filter(
            MiniSession.id == session_id,
            MiniSession.uid == uid
        ).first()
        
        if not session:
            return JSONResponse({"error": "Mini-session not found"}, status_code=404)
        
        waitlist = db.query(Waitlist).filter(
            Waitlist.mini_session_id == session_id
        ).order_by(Waitlist.created_at.asc()).all()
        
        return {"waitlist": [w.to_dict() for w in waitlist]}
    finally:
        db.close()


@router.delete("/waitlist/{entry_id}")
async def remove_from_waitlist(request: Request, entry_id: str):
    """Remove someone from waitlist"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    db: Session = next(get_db())
    try:
        entry = db.query(Waitlist).filter(
            Waitlist.id == entry_id,
            Waitlist.uid == uid
        ).first()
        
        if not entry:
            return JSONResponse({"error": "Waitlist entry not found"}, status_code=404)
        
        db.delete(entry)
        db.commit()
        return {"ok": True}
    finally:
        db.close()
