"""
List of known temporary/disposable email domains to block during signup.
This helps prevent spam and fake accounts.
"""

# Common temporary email domains
TEMP_EMAIL_DOMAINS = {
    # Popular disposable email services
    '10minutemail.com', '10minutemail.net', '10minutemail.org',
    'guerrillamail.com', 'guerrillamail.net', 'guerrillamail.org',
    'mailinator.com', 'mailinator.net', 'mailinator2.com',
    'temp-mail.org', 'temp-mail.io', 'tempmail.com',
    'throwaway.email', 'throwawaymmail.com',
    'getnada.com', 'getairmail.com',
    'fakeinbox.com', 'trashmail.com',
    'maildrop.cc', 'sharklasers.com',
    'yopmail.com', 'yopmail.fr', 'yopmail.net',
    'dispostable.com', 'spamgourmet.com',
    'mohmal.com', 'mytemp.email',
    'burnermail.io', 'emailondeck.com',
    'mintemail.com', 'mytrashmail.com',
    
    # TempMail variations
    'tmpmail.org', 'tmpmail.net',
    'tempail.com', 'tempemail.com',
    'tempinbox.com', 'tempmail.net',
    
    # Guerrilla Mail variations
    'grr.la', 'guerrillamailblock.com',
    'pokemail.net', 'spam4.me',
    
    # Mailinator variations
    'binkmail.com', 'bobmail.info',
    'chammy.info', 'devnullmail.com',
    'letthemeatspam.com', 'mailinater.com',
    'soodonims.com', 'spamhereplease.com',
    'tradermail.info',
    
    # Other common services
    'crazymailing.com', 'mailcatch.com',
    'mailexpire.com', 'mailforspam.com',
    'mailfreeonline.com', 'mailmetrash.com',
    'mailtothis.com', 'mailzi.ru',
    'nospam.ze.tc', 'nospamfor.us',
    'objectmail.com', 'proxymail.eu',
    'rcpt.at', 'rtrtr.com',
    'shortmail.net', 'sneakemail.com',
    'spam.la', 'spamavert.com',
    'speed.1s.fr', 'tafmail.com',
    'teleworm.us', 'tempm.com',
    'thanksnospam.info', 'trash2009.com',
    'wegwerfmail.de', 'wegwerfmail.net',
    'wegwerfmail.org', 'wh4f.org',
    'whyspam.me', 'willselfdestruct.com',
    'winemaven.info', 'wronghead.com',
    'zoemail.org',
    
    # Numeric/pattern-based services
    '0815.ru', '0clickemail.com',
    '1chuan.com', '1mail.ml',
    '20email.eu', '33mail.com',
    
    # Additional disposable services
    'anonbox.net', 'anonymbox.com',
    'beefmilk.com', 'bsnow.net',
    'bugmenot.com', 'deadaddress.com',
    'despam.it', 'disposeamail.com',
    'dispostable.com', 'dodgeit.com',
    'e4ward.com', 'emailias.com',
    'emltmp.com', 'filzmail.com',
    'hidemail.de', 'incognitomail.com',
    'jetable.org', 'kasmail.com',
    'koszmail.pl', 'link2mail.net',
    'no-spam.ws', 'nowhere.org',
    'pookmail.com', 'privacy.net',
    'recyclemail.dk', 'rppkn.com',
    'safe-mail.net', 'selfdestructingmail.com',
    'spambox.us', 'spamex.com',
    'spamfree24.com', 'spamfree24.de',
    'spamfree24.org', 'spamspot.com',
    'supergreatmail.com', 'tempemail.net',
    'tempinbox.co.uk', 'tempmail.it',
    'temporaryemail.net', 'temporaryinbox.com',
    'thanksnospam.info', 'veryrealemail.com',
    'vfemail.net', 'wasteland.rfc822.org',
    'wetrainbayarea.com', 'whatiaas.com',
    'xemaps.com', 'xents.com',
    'yuurok.com',
}


def is_temp_email_domain(domain: str) -> bool:
    """
    Check if the given domain is a known temporary/disposable email service.
    
    Args:
        domain: Email domain to check (e.g., 'example.com')
    
    Returns:
        True if the domain is in the blocklist, False otherwise
    """
    return domain.lower().strip() in TEMP_EMAIL_DOMAINS
