"""Email and SMS delivery for the portal: verification codes, password resets and status alerts.

Standard library only. Which channel is used depends on the environment:

  Email  BREVO_API_KEY + EMAIL_FROM             -> Brevo transactional email API
         SMTP_HOST (+ SMTP_PORT, SMTP_USER,
         SMTP_PASSWORD, EMAIL_FROM)             -> any SMTP server (STARTTLS)
         neither                                -> not sent; recorded as "logged" so the
                                                   administrator's message log shows it
  SMS    SMS_API_URL (+ SMS_API_KEY, SMS_SENDER) -> HTTP POST {"to","from","message"} as JSON
                                                   with "Authorization: Bearer <key>"
         not set                                -> recorded as "logged"

Every message is recorded by the caller (see OutboundMessage in app.py), whatever happened.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

TIMEOUT = 10  # seconds; a slow provider must never hang a request for long

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_RE = re.compile(r'^\+?[0-9]{7,15}$')


def normalise_phone(raw: str) -> str:
    """Keep digits and a leading +. Botswana numbers given locally (7 or 8 digits) get +267."""
    raw = (raw or '').strip()
    digits = re.sub(r'[^0-9]', '', raw)
    if not digits:
        return ''
    if raw.startswith('+'):
        return '+' + digits
    if digits.startswith('00'):
        return '+' + digits[2:]
    if len(digits) in (7, 8):
        return '+267' + digits
    return '+' + digits if digits.startswith('267') else digits


def valid_email(addr: str) -> bool:
    return bool(addr) and len(addr) <= 200 and bool(EMAIL_RE.match(addr))


def valid_phone(num: str) -> bool:
    return bool(num) and bool(PHONE_RE.match(num))


def mask_email(addr: str) -> str:
    """t***@example.com, so a screen can confirm where a code went without showing the address."""
    if '@' not in (addr or ''):
        return ''
    name, domain = addr.split('@', 1)
    return (name[:1] or '*') + '***@' + domain


def email_channel() -> str:
    if os.getenv('BREVO_API_KEY') and os.getenv('EMAIL_FROM'):
        return 'brevo'
    if os.getenv('SMTP_HOST'):
        return 'smtp'
    return 'log'


def sms_channel() -> str:
    return 'http' if os.getenv('SMS_API_URL') else 'log'


def send_email(to: str, subject: str, text: str) -> tuple[str, str]:
    """Returns (status, detail): status is 'sent', 'logged' or 'failed'."""
    channel = email_channel()
    if channel == 'log':
        return 'logged', 'No email service is configured; the message is kept in the log only.'
    sender = os.getenv('EMAIL_FROM', '')
    name = os.getenv('EMAIL_FROM_NAME', 'CEDA Innovation Management Portal')
    try:
        if channel == 'brevo':
            body = json.dumps({'sender': {'email': sender, 'name': name}, 'to': [{'email': to}],
                               'subject': subject, 'textContent': text}).encode()
            req = urllib.request.Request('https://api.brevo.com/v3/smtp/email', data=body, method='POST', headers={
                'api-key': os.environ['BREVO_API_KEY'], 'content-type': 'application/json', 'accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return 'sent', f'brevo {r.status}'
        msg = EmailMessage()
        msg['From'], msg['To'], msg['Subject'] = f'{name} <{sender}>', to, subject
        msg.set_content(text)
        port = int(os.getenv('SMTP_PORT', '587'))
        with smtplib.SMTP(os.environ['SMTP_HOST'], port, timeout=TIMEOUT) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if os.getenv('SMTP_USER'):
                smtp.login(os.environ['SMTP_USER'], os.getenv('SMTP_PASSWORD', ''))
            smtp.send_message(msg)
        return 'sent', f'smtp {os.environ["SMTP_HOST"]}'
    except (urllib.error.URLError, OSError, smtplib.SMTPException, KeyError) as exc:
        return 'failed', f'{type(exc).__name__}: {str(exc)[:200]}'


def send_sms(to: str, text: str) -> tuple[str, str]:
    """Returns (status, detail): status is 'sent', 'logged' or 'failed'."""
    if sms_channel() == 'log':
        return 'logged', 'No SMS gateway is configured; the message is kept in the log only.'
    body = json.dumps({'to': to, 'from': os.getenv('SMS_SENDER', 'CEDA'), 'message': text[:480]}).encode()
    headers = {'content-type': 'application/json'}
    if os.getenv('SMS_API_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['SMS_API_KEY']
    try:
        req = urllib.request.Request(os.environ['SMS_API_URL'], data=body, method='POST', headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return 'sent', f'gateway {r.status}'
    except (urllib.error.URLError, OSError, KeyError) as exc:
        return 'failed', f'{type(exc).__name__}: {str(exc)[:200]}'
