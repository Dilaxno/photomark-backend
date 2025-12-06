"""
IPTC/EXIF Metadata Utility

Embeds copyright and contact information into photos using piexif.
Supports IPTC-like fields via EXIF tags for maximum compatibility.
"""

import io
from datetime import datetime
from typing import Optional
from PIL import Image

try:
    import piexif
    PIEXIF_AVAILABLE = True
except ImportError:
    PIEXIF_AVAILABLE = False

from core.config import logger


class MetadataSettings:
    """Container for metadata settings to embed in photos."""
    
    def __init__(
        self,
        photographer_name: Optional[str] = None,
        copyright_notice: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
        contact_website: Optional[str] = None,
        business_name: Optional[str] = None,
        address: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
    ):
        self.photographer_name = photographer_name
        self.copyright_notice = copyright_notice
        self.contact_email = contact_email
        self.contact_phone = contact_phone
        self.contact_website = contact_website
        self.business_name = business_name
        self.address = address
        self.city = city
        self.country = country
    
    def to_dict(self) -> dict:
        return {
            "photographer_name": self.photographer_name,
            "copyright_notice": self.copyright_notice,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "contact_website": self.contact_website,
            "business_name": self.business_name,
            "address": self.address,
            "city": self.city,
            "country": self.country,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "MetadataSettings":
        return cls(
            photographer_name=data.get("photographer_name"),
            copyright_notice=data.get("copyright_notice"),
            contact_email=data.get("contact_email"),
            contact_phone=data.get("contact_phone"),
            contact_website=data.get("contact_website"),
            business_name=data.get("business_name"),
            address=data.get("address"),
            city=data.get("city"),
            country=data.get("country"),
        )


def build_exif_dict(settings: MetadataSettings) -> dict:
    """
    Build an EXIF dictionary from metadata settings.
    Uses standard EXIF/TIFF tags for maximum compatibility.
    """
    if not PIEXIF_AVAILABLE:
        return {}
    
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}}
    
    # Artist (photographer name)
    if settings.photographer_name:
        exif_dict["0th"][piexif.ImageIFD.Artist] = settings.photographer_name.encode('utf-8')
    
    # Copyright notice
    if settings.copyright_notice:
        exif_dict["0th"][piexif.ImageIFD.Copyright] = settings.copyright_notice.encode('utf-8')
    else:
        # Auto-generate copyright if photographer name is set
        if settings.photographer_name:
            year = datetime.utcnow().year
            auto_copyright = f"© {year} {settings.photographer_name}. All rights reserved."
            exif_dict["0th"][piexif.ImageIFD.Copyright] = auto_copyright.encode('utf-8')
    
    # Software tag (for branding)
    exif_dict["0th"][piexif.ImageIFD.Software] = b"Photomark"
    
    # ImageDescription - can include contact info
    description_parts = []
    if settings.business_name:
        description_parts.append(settings.business_name)
    if settings.contact_website:
        description_parts.append(settings.contact_website)
    if settings.contact_email:
        description_parts.append(settings.contact_email)
    
    if description_parts:
        exif_dict["0th"][piexif.ImageIFD.ImageDescription] = " | ".join(description_parts).encode('utf-8')
    
    # XPAuthor (Windows-specific but widely supported)
    if settings.photographer_name:
        try:
            exif_dict["0th"][piexif.ImageIFD.XPAuthor] = settings.photographer_name.encode('utf-16le')
        except Exception:
            pass
    
    # XPComment for additional contact info
    contact_parts = []
    if settings.contact_email:
        contact_parts.append(f"Email: {settings.contact_email}")
    if settings.contact_phone:
        contact_parts.append(f"Phone: {settings.contact_phone}")
    if settings.contact_website:
        contact_parts.append(f"Web: {settings.contact_website}")
    if settings.address:
        addr = settings.address
        if settings.city:
            addr += f", {settings.city}"
        if settings.country:
            addr += f", {settings.country}"
        contact_parts.append(f"Address: {addr}")
    
    if contact_parts:
        try:
            exif_dict["0th"][piexif.ImageIFD.XPComment] = " | ".join(contact_parts).encode('utf-16le')
        except Exception:
            pass
    
    return exif_dict


def embed_metadata(
    image: Image.Image,
    settings: MetadataSettings,
    preserve_existing: bool = True
) -> tuple[Image.Image, bytes]:
    """
    Embed EXIF metadata into an image.
    
    Args:
        image: PIL Image object
        settings: MetadataSettings with copyright/contact info
        preserve_existing: If True, merge with existing EXIF data
    
    Returns:
        Tuple of (image, jpeg_bytes) with embedded metadata
    """
    if not PIEXIF_AVAILABLE:
        logger.warning("piexif not available, skipping metadata embedding")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=95)
        buf.seek(0)
        return image, buf.getvalue()
    
    try:
        # Build new EXIF data
        new_exif = build_exif_dict(settings)
        
        # Try to preserve existing EXIF if requested
        if preserve_existing:
            try:
                existing_exif = piexif.load(image.info.get("exif", b""))
                # Merge: new values override existing
                for ifd in ("0th", "Exif", "GPS", "1st", "Interop"):
                    if ifd in existing_exif and existing_exif[ifd]:
                        for tag, value in existing_exif[ifd].items():
                            if tag not in new_exif.get(ifd, {}):
                                if ifd not in new_exif:
                                    new_exif[ifd] = {}
                                new_exif[ifd][tag] = value
            except Exception as e:
                logger.debug(f"Could not load existing EXIF: {e}")
        
        # Dump EXIF to bytes
        exif_bytes = piexif.dump(new_exif)
        
        # Save image with new EXIF
        buf = io.BytesIO()
        image.save(
            buf,
            format="JPEG",
            quality=95,
            subsampling=0,
            progressive=True,
            optimize=True,
            exif=exif_bytes
        )
        buf.seek(0)
        
        return image, buf.getvalue()
    
    except Exception as e:
        logger.warning(f"Failed to embed metadata: {e}")
        # Fallback: save without metadata
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=95)
        buf.seek(0)
        return image, buf.getvalue()


def embed_metadata_to_bytes(
    image_bytes: bytes,
    settings: MetadataSettings,
    preserve_existing: bool = True
) -> bytes:
    """
    Embed EXIF metadata into image bytes.
    
    Args:
        image_bytes: Raw image bytes
        settings: MetadataSettings with copyright/contact info
        preserve_existing: If True, merge with existing EXIF data
    
    Returns:
        JPEG bytes with embedded metadata
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        _, result_bytes = embed_metadata(img, settings, preserve_existing)
        return result_bytes
    except Exception as e:
        logger.warning(f"Failed to embed metadata to bytes: {e}")
        return image_bytes


def read_metadata(image_bytes: bytes) -> dict:
    """
    Read EXIF metadata from image bytes.
    
    Returns dict with extracted metadata fields.
    """
    if not PIEXIF_AVAILABLE:
        return {}
    
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif_data = img.info.get("exif", b"")
        if not exif_data:
            return {}
        
        exif_dict = piexif.load(exif_data)
        result = {}
        
        # Extract Artist
        if piexif.ImageIFD.Artist in exif_dict.get("0th", {}):
            val = exif_dict["0th"][piexif.ImageIFD.Artist]
            result["artist"] = val.decode('utf-8', errors='ignore') if isinstance(val, bytes) else str(val)
        
        # Extract Copyright
        if piexif.ImageIFD.Copyright in exif_dict.get("0th", {}):
            val = exif_dict["0th"][piexif.ImageIFD.Copyright]
            result["copyright"] = val.decode('utf-8', errors='ignore') if isinstance(val, bytes) else str(val)
        
        # Extract ImageDescription
        if piexif.ImageIFD.ImageDescription in exif_dict.get("0th", {}):
            val = exif_dict["0th"][piexif.ImageIFD.ImageDescription]
            result["description"] = val.decode('utf-8', errors='ignore') if isinstance(val, bytes) else str(val)
        
        # Extract Software
        if piexif.ImageIFD.Software in exif_dict.get("0th", {}):
            val = exif_dict["0th"][piexif.ImageIFD.Software]
            result["software"] = val.decode('utf-8', errors='ignore') if isinstance(val, bytes) else str(val)
        
        return result
    
    except Exception as e:
        logger.debug(f"Failed to read metadata: {e}")
        return {}
