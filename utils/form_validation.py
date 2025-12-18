"""
Form Validation Utilities
CleanEnroll-style validation for booking forms
Includes email validation, bot protection, spam detection, and field validation
"""
import re
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any

logger = logging.getLogger(__name__)

# ============================================================================
# FREE EMAIL PROVIDERS (for professional email validation)
# ============================================================================
FREE_EMAIL_PROVIDERS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'rocketmail.com',
    'outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'aol.com',
    'icloud.com', 'me.com', 'mac.com', 'protonmail.com', 'pm.me',
    'mail.com', 'gmx.com', 'gmx.net', 'zoho.com',
    'yandex.com', 'yandex.ru', 'inbox.ru', 'list.ru', 'bk.ru', 'mail.ru'
}

# ============================================================================
# ROLE-BASED EMAIL PREFIXES (generic inboxes to block)
# ============================================================================
ROLE_EMAIL_PREFIXES = {
    'admin', 'administrator', 'info', 'contact', 'support', 'help',
    'sales', 'marketing', 'billing', 'accounts', 'hr', 'jobs',
    'careers', 'press', 'media', 'webmaster', 'postmaster', 'hostmaster',
    'abuse', 'noreply', 'no-reply', 'donotreply', 'do-not-reply',
    'feedback', 'enquiries', 'enquiry', 'office', 'team', 'hello'
}

# ============================================================================
# DISPOSABLE EMAIL DOMAINS (common temporary email services)
# ============================================================================
DISPOSABLE_DOMAINS = {
    'tempmail.com', 'throwaway.email', 'guerrillamail.com', 'mailinator.com',
    '10minutemail.com', 'temp-mail.org', 'fakeinbox.com', 'trashmail.com',
    'getnada.com', 'mohmal.com', 'dispostable.com', 'maildrop.cc',
    'yopmail.com', 'sharklasers.com', 'guerrillamail.info', 'grr.la',
    'spam4.me', 'tempail.com', 'emailondeck.com', 'mintemail.com'
}


# ============================================================================
# EMAIL VALIDATION
# ============================================================================

def validate_email_format(email: str) -> Tuple[bool, str]:
    """Basic email format validation"""
    if not email:
        return False, "Email is required"
    
    email = email.strip().lower()
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    
    if not re.match(pattern, email):
        return False, "Please enter a valid email address"
    
    return True, ""


def is_free_email_provider(email: str) -> bool:
    """Check if email is from a free provider"""
    try:
        domain = email.split('@')[1].lower()
        return domain in FREE_EMAIL_PROVIDERS
    except:
        return False


def is_role_based_email(email: str) -> bool:
    """Check if email is a role-based/generic inbox"""
    try:
        local_part = email.split('@')[0].lower()
        return local_part in ROLE_EMAIL_PREFIXES
    except:
        return False


def is_disposable_email(email: str) -> bool:
    """Check if email is from a disposable email service"""
    try:
        domain = email.split('@')[1].lower()
        return domain in DISPOSABLE_DOMAINS
    except:
        return False


def detect_gibberish_email(email: str) -> Tuple[bool, str]:
    """
    Detect gibberish/random email addresses
    Uses heuristics like consonant clusters, repeated chars, entropy
    """
    try:
        local_part = email.split('@')[0].lower()
        
        # Remove common separators
        clean = re.sub(r'[._-]', '', local_part)
        
        if len(clean) < 3:
            return False, ""

        # Check for excessive consonant clusters (4+ consonants in a row)
        consonant_clusters = re.findall(r'[bcdfghjklmnpqrstvwxyz]{4,}', clean)
        if len(consonant_clusters) >= 2:
            return True, "This email address appears to be invalid"
        
        # Check for repeated characters (3+ same char)
        if re.search(r'(.)\1{2,}', clean):
            return True, "This email address appears to be invalid"
        
        # Check vowel ratio (very low vowel ratio suggests gibberish)
        vowels = len(re.findall(r'[aeiou]', clean))
        if len(clean) > 5 and vowels / len(clean) < 0.1:
            return True, "This email address appears to be invalid"
        
        # Check for keyboard patterns
        keyboard_patterns = ['qwerty', 'asdf', 'zxcv', '1234', 'abcd']
        for pattern in keyboard_patterns:
            if pattern in clean:
                return True, "This email address appears to be invalid"
        
        return False, ""
    except Exception as e:
        logger.warning(f"Gibberish detection error: {e}")
        return False, ""


def verify_email_domain(email: str) -> Tuple[bool, str]:
    """Verify email domain has valid MX records"""
    try:
        import dns.resolver
        domain = email.split('@')[1]
        
        # Check MX records
        try:
            mx_records = dns.resolver.resolve(domain, 'MX')
            if mx_records:
                return True, ""
        except dns.resolver.NoAnswer:
            pass
        except dns.resolver.NXDOMAIN:
            return False, f"The domain '{domain}' does not exist"
        except Exception:
            pass
        
        # Fallback: check A record
        try:
            a_records = dns.resolver.resolve(domain, 'A')
            if a_records:
                return True, ""
        except:
            pass
        
        return False, f"The domain '{domain}' cannot receive emails"
    except ImportError:
        logger.warning("dnspython not installed, skipping MX verification")
        return True, ""
    except Exception as e:
        logger.warning(f"Email domain verification failed: {e}")
        return True, ""  # Allow on error to not block legitimate users


def validate_email_comprehensive(
    email: str,
    verify_domain: bool = False,
    detect_gibberish: bool = False,
    professional_only: bool = False,
    block_role_emails: bool = False,
    block_disposable: bool = True
) -> Tuple[bool, str]:
    """
    Comprehensive email validation with all checks
    Returns (is_valid, error_message)
    """
    email = email.strip().lower()
    
    # Basic format check
    is_valid, error = validate_email_format(email)
    if not is_valid:
        return False, error
    
    # Disposable email check
    if block_disposable and is_disposable_email(email):
        return False, "Please use a permanent email address"
    
    # Professional email check
    if professional_only and is_free_email_provider(email):
        return False, "Please use a professional/business email address"
    
    # Role-based email check
    if block_role_emails and is_role_based_email(email):
        return False, "Please use a personal email address, not a generic inbox"
    
    # Gibberish detection
    if detect_gibberish:
        is_gibberish, gibberish_msg = detect_gibberish_email(email)
        if is_gibberish:
            return False, gibberish_msg
    
    # Domain verification (MX check)
    if verify_domain:
        is_valid, verify_msg = verify_email_domain(email)
        if not is_valid:
            return False, verify_msg
    
    return True, ""


# ============================================================================
# FIELD VALIDATION
# ============================================================================

def validate_required_field(value: Any, field_type: str) -> bool:
    """Check if a required field has a valid value"""
    # Display-only fields are never required
    if field_type in ['heading1', 'heading2', 'heading3', 'image', 'video', 'audio']:
        return True
    
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == '':
        return False
    if isinstance(value, list) and len(value) == 0:
        return False
    if isinstance(value, bool) and value is False:
        return False
    
    return True


def validate_phone(phone: str) -> Tuple[bool, str]:
    """Validate phone number format"""
    if not phone:
        return True, ""
    
    # Remove common formatting characters
    clean = re.sub(r'[\s\-\.\(\)]+', '', phone)
    
    # Should be mostly digits, optionally starting with +
    if not re.match(r'^\+?[0-9]{7,15}$', clean):
        return False, "Please enter a valid phone number"
    
    return True, ""


def validate_url(url: str) -> Tuple[bool, str]:
    """Validate URL format"""
    if not url:
        return True, ""
    
    url = url.strip()
    
    # Must start with http:// or https://
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return False, "URL must start with http:// or https://"
    
    # Basic URL pattern
    pattern = r'^https?://[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*'
    if not re.match(pattern, url):
        return False, "Please enter a valid URL"
    
    return True, ""


def validate_number(value: Any, min_val: float = None, max_val: float = None) -> Tuple[bool, str]:
    """Validate numeric value"""
    try:
        num = float(value)
        if min_val is not None and num < min_val:
            return False, f"Value must be at least {min_val}"
        if max_val is not None and num > max_val:
            return False, f"Value must be at most {max_val}"
        return True, ""
    except (ValueError, TypeError):
        return False, "Please enter a valid number"


def validate_date(value: str) -> Tuple[bool, str]:
    """Validate date format"""
    if not value:
        return True, ""
    
    try:
        # Try ISO format
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True, ""
    except:
        pass
    
    # Try common formats
    formats = ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y']
    for fmt in formats:
        try:
            datetime.strptime(value, fmt)
            return True, ""
        except:
            continue
    
    return False, "Please enter a valid date"


def validate_field(
    field: Dict[str, Any],
    value: Any,
    form_settings: Dict[str, Any] = None
) -> Tuple[bool, str]:
    """
    Validate a single field value based on field type and settings
    Returns (is_valid, error_message)
    """
    field_type = field.get('type', 'text')
    field_label = field.get('label', 'Field')
    required = field.get('required', False)
    settings = field.get('settings', {})
    form_settings = form_settings or {}
    
    # Check required
    if required and not validate_required_field(value, field_type):
        return False, f"{field_label} is required"
    
    # Skip further validation if empty and not required
    if not value or (isinstance(value, str) and not value.strip()):
        return True, ""
    
    # Type-specific validation
    if field_type == 'email':
        return validate_email_comprehensive(
            str(value),
            verify_domain=settings.get('verifyEmail') or form_settings.get('verify_email_domain', False),
            detect_gibberish=settings.get('detectGibberish') or form_settings.get('detect_gibberish_email', False),
            professional_only=form_settings.get('professional_emails_only', False),
            block_role_emails=form_settings.get('block_role_emails', False),
            block_disposable=True
        )
    
    elif field_type == 'phone':
        return validate_phone(str(value))
    
    elif field_type == 'url':
        return validate_url(str(value))
    
    elif field_type == 'number':
        min_val = settings.get('min')
        max_val = settings.get('max')
        return validate_number(value, min_val, max_val)
    
    elif field_type == 'date' or field_type == 'time':
        return validate_date(str(value))
    
    elif field_type == 'text' or field_type == 'textarea':
        # Check max length
        max_length = settings.get('maxLength')
        if max_length and len(str(value)) > max_length:
            return False, f"{field_label} must be {max_length} characters or less"
        
        # Check min length
        min_length = settings.get('minLength')
        if min_length and len(str(value)) < min_length:
            return False, f"{field_label} must be at least {min_length} characters"
    
    elif field_type == 'full-name':
        # Check for at least two words
        if settings.get('requireTwoWords', True):
            words = str(value).strip().split()
            if len(words) < 2:
                return False, "Please enter your full name (first and last)"
    
    elif field_type == 'password':
        password = str(value)
        min_length = settings.get('minLength', 8)
        
        if len(password) < min_length:
            return False, f"Password must be at least {min_length} characters"
        
        if settings.get('requireUppercase') and not re.search(r'[A-Z]', password):
            return False, "Password must contain at least one uppercase letter"
        
        if settings.get('requireLowercase') and not re.search(r'[a-z]', password):
            return False, "Password must contain at least one lowercase letter"
        
        if settings.get('requireNumber') and not re.search(r'[0-9]', password):
            return False, "Password must contain at least one number"
        
        if settings.get('requireSpecial') and not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            return False, "Password must contain at least one special character"
    
    return True, ""


# ============================================================================
# BOT PROTECTION & SPAM DETECTION
# ============================================================================

def check_honeypot(data: Dict[str, Any], honeypot_field: str = "_hp_field") -> bool:
    """
    Check honeypot field - should be empty if human
    Returns True if submission is likely from a bot
    """
    honeypot_value = data.get(honeypot_field, "")
    return bool(honeypot_value)


def check_submission_time(
    form_load_time: datetime,
    submission_time: datetime,
    min_seconds: int = 3
) -> Tuple[bool, int]:
    """
    Check if form was filled too quickly (bot behavior)
    Returns (is_suspicious, time_taken_seconds)
    """
    if not form_load_time or not submission_time:
        return False, 0
    
    time_taken = (submission_time - form_load_time).total_seconds()
    is_suspicious = time_taken < min_seconds
    
    return is_suspicious, int(time_taken)


def calculate_spam_score(
    data: Dict[str, Any],
    ip_address: str = None,
    user_agent: str = None,
    time_to_complete: int = None,
    min_submission_time: int = 3
) -> float:
    """
    Calculate spam score (0.0 to 1.0)
    Higher score = more likely spam
    """
    score = 0.0
    
    # Check for honeypot
    if check_honeypot(data):
        score += 0.5
    
    # Check submission time
    if time_to_complete is not None and time_to_complete < min_submission_time:
        score += 0.3
    
    # Check for suspicious patterns in text fields
    all_text = " ".join(str(v) for v in data.values() if isinstance(v, str))
    
    # Multiple URLs in text
    url_count = len(re.findall(r'https?://', all_text))
    if url_count > 3:
        score += 0.2
    
    # Excessive caps
    if len(all_text) > 20:
        caps_ratio = sum(1 for c in all_text if c.isupper()) / len(all_text)
        if caps_ratio > 0.5:
            score += 0.1
    
    # Missing user agent (bot indicator)
    if not user_agent:
        score += 0.1
    
    return min(score, 1.0)


# ============================================================================
# DUPLICATE PREVENTION
# ============================================================================

def generate_submission_hash(
    form_id: str,
    email: str = None,
    ip_address: str = None
) -> str:
    """Generate a hash for duplicate detection"""
    parts = [str(form_id)]
    if email:
        parts.append(email.lower().strip())
    if ip_address:
        parts.append(ip_address)
    
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


# ============================================================================
# GEO RESTRICTION
# ============================================================================

def check_geo_restriction(
    country_code: str,
    allowed_countries: List[str] = None,
    restricted_countries: List[str] = None
) -> Tuple[bool, str]:
    """
    Check if submission is allowed based on country
    Returns (is_allowed, error_message)
    """
    if not country_code:
        return True, ""
    
    country_code = country_code.upper()
    
    # Check allowed list (whitelist)
    if allowed_countries and len(allowed_countries) > 0:
        allowed_upper = [c.upper() for c in allowed_countries]
        if country_code not in allowed_upper:
            return False, "Submissions from your region are not accepted"
    
    # Check restricted list (blacklist)
    if restricted_countries and len(restricted_countries) > 0:
        restricted_upper = [c.upper() for c in restricted_countries]
        if country_code in restricted_upper:
            return False, "Submissions from your region are not accepted"
    
    return True, ""


# ============================================================================
# FORM SUBMISSION VALIDATION
# ============================================================================

def validate_form_submission(
    form: Any,
    data: Dict[str, Any],
    ip_address: str = None,
    user_agent: str = None,
    country_code: str = None,
    form_load_time: datetime = None
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """
    Comprehensive form submission validation
    Returns (is_valid, errors, metadata)
    """
    errors = []
    metadata = {
        "spam_score": 0.0,
        "is_spam": False,
        "time_to_complete": None,
        "validation_errors": []
    }
    
    submission_time = datetime.utcnow()
    
    # Calculate time to complete
    if form_load_time:
        time_diff = (submission_time - form_load_time).total_seconds()
        metadata["time_to_complete"] = int(time_diff)
    
    # Check submission limit
    if form.submission_limit and form.submission_limit > 0:
        if form.submissions_count >= form.submission_limit:
            errors.append("This form is no longer accepting submissions")
            return False, errors, metadata

    # Geo restriction check
    if country_code:
        is_allowed, geo_error = check_geo_restriction(
            country_code,
            form.allowed_countries or [],
            form.restricted_countries or []
        )
        if not is_allowed:
            errors.append(geo_error)
            return False, errors, metadata
    
    # Honeypot check
    if form.honeypot_enabled and check_honeypot(data):
        metadata["is_spam"] = True
        metadata["spam_score"] = 1.0
        # Silently accept but mark as spam
        return True, [], metadata
    
    # Time-based check
    if form.time_based_check_enabled and metadata["time_to_complete"]:
        min_time = form.min_submission_time or 3
        if metadata["time_to_complete"] < min_time:
            metadata["spam_score"] += 0.5
    
    # Calculate overall spam score
    metadata["spam_score"] = calculate_spam_score(
        data,
        ip_address=ip_address,
        user_agent=user_agent,
        time_to_complete=metadata["time_to_complete"],
        min_submission_time=form.min_submission_time or 3
    )
    
    if metadata["spam_score"] >= 0.7:
        metadata["is_spam"] = True
    
    # Build form settings dict for field validation
    form_settings = {
        "verify_email_domain": form.verify_email_domain,
        "detect_gibberish_email": form.detect_gibberish_email,
        "professional_emails_only": form.professional_emails_only,
        "block_role_emails": form.block_role_emails,
    }
    
    # Validate each field
    for field in form.fields or []:
        field_id = field.get("id")
        value = data.get(field_id)
        
        is_valid, error = validate_field(field, value, form_settings)
        if not is_valid:
            errors.append(error)
            metadata["validation_errors"].append({
                "field_id": field_id,
                "field_label": field.get("label"),
                "error": error
            })
    
    return len(errors) == 0, errors, metadata
