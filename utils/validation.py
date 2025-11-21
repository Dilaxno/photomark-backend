"""
Validation utilities for user input (names, emails)
Prevents spam and gibberish submissions
"""
import re
import dns.resolver
from typing import Tuple


def is_gibberish(text: str) -> bool:
    """
    Detect gibberish text by analyzing patterns.
    Returns True if text appears to be gibberish.
    """
    if not text:
        return True
    
    # Clean to only letters
    cleaned = re.sub(r'[^a-z]', '', text.lower())
    if len(cleaned) < 3:
        return True
    
    # Check for excessive consonant clusters (5+ in a row)
    consonant_cluster = re.compile(r'[bcdfghjklmnpqrstvwxyz]{5,}', re.IGNORECASE)
    if consonant_cluster.search(cleaned):
        return True
    
    # Check for lack of vowels (less than 20% is suspicious)
    vowels = len(re.findall(r'[aeiou]', cleaned))
    vowel_ratio = vowels / len(cleaned) if len(cleaned) > 0 else 0
    if vowel_ratio < 0.2:
        return True
    
    # Check for repeating patterns like "fghfgh" or "abcabc"
    for i in range(2, len(cleaned) // 2 + 1):
        chunk = cleaned[:i]
        pattern = re.compile(f'^({re.escape(chunk)}){{2,}}')
        if pattern.match(cleaned[:i * 2]):
            return True
    
    return False


def validate_full_name(name: str) -> Tuple[bool, str]:
    """
    Validate full name (must have first and last name, no gibberish).
    Returns (is_valid, error_message).
    """
    trimmed = name.strip()
    
    if not trimmed:
        return False, "Full name is required"
    
    # Must have at least 2 words (first and last name)
    words = [w for w in re.split(r'\s+', trimmed) if len(w) > 0]
    if len(words) < 2:
        return False, "Please enter both first and last name"
    
    # Each word must be at least 2 characters
    if any(len(w) < 2 for w in words):
        return False, "Each name must be at least 2 characters"
    
    # Check each word for gibberish
    for word in words:
        if is_gibberish(word):
            return False, f"Invalid name detected: '{word}' appears to be gibberish"
    
    return True, ""


def validate_email(email: str) -> Tuple[bool, str]:
    """
    Validate email address (proper format, no gibberish in local part).
    Returns (is_valid, error_message).
    """
    trimmed = email.strip().lower()
    
    if not trimmed:
        return False, "Email is required"
    
    # Basic email format check
    email_regex = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    if not email_regex.match(trimmed):
        return False, "Invalid email format"
    
    # Extract local part (before @)
    local_part = trimmed.split('@')[0]
    
    # Check if local part is gibberish
    # Remove common valid characters (dots, dashes, underscores, numbers)
    local_alpha = re.sub(r'[._\-0-9]', '', local_part)
    if local_alpha and is_gibberish(local_alpha):
        return False, f"Invalid email: '{local_part}' appears to be gibberish"
    
    return True, ""


def validate_email_mx(email: str) -> Tuple[bool, str]:
    """
    Validate email domain has valid MX records.
    Returns (is_valid, error_message).
    """
    trimmed = email.strip().lower()
    
    if not trimmed or '@' not in trimmed:
        return False, "Invalid email format"
    
    domain = trimmed.split('@')[-1]
    
    try:
        # Query MX records for the domain
        mx_records = dns.resolver.resolve(domain, 'MX')
        if not mx_records:
            return False, f"No mail server found for domain '{domain}'"
        return True, ""
    except dns.resolver.NXDOMAIN:
        return False, f"Domain '{domain}' does not exist"
    except dns.resolver.NoAnswer:
        return False, f"No mail server configured for '{domain}'"
    except dns.resolver.Timeout:
        # Don't fail on timeout - could be network issue
        return True, ""
    except Exception as e:
        # Don't fail on other DNS errors - could be temporary
        return True, ""


def validate_signup_data(name: str, email: str) -> Tuple[bool, str]:
    """
    Validate signup data (both name and email).
    Returns (is_valid, error_message).
    """
    # Validate name
    name_valid, name_error = validate_full_name(name)
    if not name_valid:
        return False, name_error
    
    # Validate email
    email_valid, email_error = validate_email(email)
    if not email_valid:
        return False, email_error
    
    return True, ""
