"""FundingLens AI - institutional funding assessment API.

Single source of truth for the version is the VERSION file at the repository root.
Pure analysis logic lives in backend/analysis.py (unit-tested without a database).
"""
import hashlib
import hmac
import base64
import html as htmllib
import io
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint,
                        create_engine, func, inspect as sa_inspect, or_, select, text as sqltext)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import analysis as A
from . import messaging as MSG

try:
    from openai import OpenAI
except Exception:  # the openai package is optional at runtime
    OpenAI = None

log = logging.getLogger('fundinglens')

# ----------------------------------------------------------------------------
# Configuration (fail closed)
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / 'VERSION').read_text().strip()
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production').lower()
IS_PROD = ENVIRONMENT == 'production'
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
DOCS = DATA / 'documents'
DOCS.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', '15000000'))
ACCESS_MINUTES = int(os.getenv('ACCESS_TOKEN_MINUTES', '60'))
ALGORITHM = 'HS256'
REGISTER_LIMIT = int(os.getenv('REGISTER_LIMIT', '5'))    # new accounts per IP per hour
LOGIN_LIMIT = int(os.getenv('LOGIN_LIMIT', '8'))          # attempts per username+IP per 5 min
LOGIN_IP_LIMIT = int(os.getenv('LOGIN_IP_LIMIT', '40'))  # attempts per IP per 5 min


def _database_url() -> str:
    url = os.getenv('DATABASE_URL', f"sqlite:///{DATA / 'fundinglens.db'}")
    # Hosting providers hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver name.
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix):]
    return url


DB_URL = _database_url()
IS_SQLITE = DB_URL.startswith('sqlite')

SECRET_KEY = os.getenv('JWT_SECRET', '')
if len(SECRET_KEY) < 32:
    if IS_PROD:
        raise RuntimeError('JWT_SECRET must be set to a random value of at least 32 characters in production.')
    SECRET_KEY = secrets.token_urlsafe(48)  # development/test only: tokens die with the process
    log.warning('JWT_SECRET not set - using a random per-process secret (ENVIRONMENT=%s).', ENVIRONMENT)

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
if len(ADMIN_PASSWORD) < 12:
    if IS_PROD:
        raise RuntimeError('ADMIN_PASSWORD must be set (at least 12 characters) in production.')
    ADMIN_PASSWORD = secrets.token_urlsafe(14)
    log.warning('ADMIN_PASSWORD not set - generated one for this process: %s', ADMIN_PASSWORD)

ALLOWED_ORIGINS = [x.strip() for x in os.getenv('ALLOWED_ORIGINS', '').split(',') if x.strip()]
if '*' in ALLOWED_ORIGINS and IS_PROD:
    raise RuntimeError('ALLOWED_ORIGINS must not contain * in production. Leave it empty for same-origin use.')

connect_args = {'check_same_thread': False} if IS_SQLITE else {}
engine = create_engine(DB_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Money = Numeric(18, 2, asdecimal=False)
ROLES = ('admin', 'officer', 'committee', 'applicant')
SELF_REGISTRATION = os.getenv('ALLOW_SELF_REGISTRATION', 'true').lower() in ('1', 'true', 'yes')
# Demo only: return verification codes in API responses when no email/SMS service is set up. Never on in production.
DEMO_SHOW_CODES = os.getenv('DEMO_SHOW_CODES', 'false').lower() in ('1', 'true', 'yes')
PORTAL_URL = os.getenv('PORTAL_URL', '').rstrip('/')
CODE_MINUTES = {'verify': 30, 'reset': 30, 'login': 10, 'email': 30}
CODE_MAX_ATTEMPTS = 5
STAFF = ('admin', 'officer', 'committee')


def utcnow() -> datetime:
    """Naive UTC. All timestamps are stored naive-UTC and emitted with a trailing Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.isoformat() + 'Z' if dt else None


def new_id() -> str:
    return str(uuid.uuid4())


# ----------------------------------------------------------------------------
# Database models
# ----------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class Institution(Base):
    __tablename__ = 'institutions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # opt-in: applicant data goes to a third party
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default='officer')
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # contact details, verification and alert preferences (1.4)
    full_name: Mapped[str] = mapped_column(String(200), default='')
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)   # self-registered accounts start False
    pending_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    otp_login: Mapped[bool] = mapped_column(Boolean, default=False)       # ask for an emailed code at every sign-in
    alert_email: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional, only used for anonymous statistics in reports (1.6)
    gender: Mapped[str] = mapped_column(String(20), default='')
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    district: Mapped[str] = mapped_column(String(80), default='')


class OneTimeCode(Base):
    """6-digit codes for email verification, password reset and sign-in. Only a keyed hash is stored."""
    __tablename__ = 'one_time_codes'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    purpose: Mapped[str] = mapped_column(String(20))      # verify | reset | login | email
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OutboundMessage(Base):
    """Every email and SMS the portal sends (or would send), for delivery and for the administrator's log."""
    __tablename__ = 'outbound_messages'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(10))      # email | sms
    kind: Mapped[str] = mapped_column(String(20))         # verify | reset | login | email | alert
    to_addr: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(200), default='')
    body: Mapped[str] = mapped_column(Text, default='')   # codes are masked here; the code itself is never stored
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)  # queued | sent | logged | failed
    detail: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FundingProgram(Base):
    __tablename__ = 'funding_programs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default='')
    min_request: Mapped[float] = mapped_column(Money, default=0)
    max_request: Mapped[float] = mapped_column(Money, default=0)
    min_owner_contribution_pct: Mapped[float] = mapped_column(Float, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RubricCriterion(Base):
    """One institution-defined standard. programme_id NULL = applies to every programme."""
    __tablename__ = 'rubric_criteria'
    __table_args__ = (UniqueConstraint('institution_id', 'programme_id', 'key', name='uq_rubric_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    programme_id: Mapped[str | None] = mapped_column(ForeignKey('funding_programs.id'), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='')
    kind: Mapped[str] = mapped_column(String(20))  # numeric | evidence | qualitative
    metric: Mapped[str | None] = mapped_column(String(60), nullable=True)
    comparator: Mapped[str | None] = mapped_column(String(3), nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    keywords_json: Mapped[str] = mapped_column(Text, default='[]')
    mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    __tablename__ = 'applications'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    programme_id: Mapped[str | None] = mapped_column(ForeignKey('funding_programs.id'), nullable=True)
    applicant_name: Mapped[str] = mapped_column(String(200))
    funding_request: Mapped[float] = mapped_column(Money, default=0)
    owner_contribution: Mapped[float] = mapped_column(Money, default=0)
    revenue_override: Mapped[float | None] = mapped_column(Money, nullable=True)
    expenses_override: Mapped[float | None] = mapped_column(Money, nullable=True)
    # Innovation-idea fields (CEDA Innovation Management Portal). Required for submitters at creation time.
    idea_title: Mapped[str] = mapped_column(String(200), default='')
    problem: Mapped[str] = mapped_column(Text, default='')
    solution: Mapped[str] = mapped_column(Text, default='')
    target_users: Mapped[str] = mapped_column(Text, default='')
    sector: Mapped[str] = mapped_column(String(80), default='')
    stage: Mapped[str] = mapped_column(String(80), default='')
    extra_json: Mapped[str] = mapped_column(Text, default='{}')  # answers to the administrator's extra form questions
    status: Mapped[str] = mapped_column(String(40), default='draft', index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    referred_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_risks: Mapped[int] = mapped_column(Integer, default=0)
    medium_risks: Mapped[int] = mapped_column(Integer, default=0)
    low_risks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    storage_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default='application/octet-stream')
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssessmentVersion(Base):
    """Every assessment is kept. Decisions reference the version they relied on."""
    __tablename__ = 'assessment_versions'
    __table_args__ = (UniqueConstraint('application_id', 'version', name='uq_assessment_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[str | None] = mapped_column(ForeignKey('documents.id'), nullable=True)
    trigger: Mapped[str] = mapped_column(String(30), default='upload')
    result_json: Mapped[str] = mapped_column(Text)
    ai_output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_mode: Mapped[str] = mapped_column(String(30), default='rules_only')
    ai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DueDiligenceQuestion(Base):
    __tablename__ = 'due_diligence_questions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    qkey: Mapped[str] = mapped_column(String(40), index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), default='general')
    rationale: Mapped[str] = mapped_column(Text, default='')
    priority: Mapped[str] = mapped_column(String(20), default='medium')
    status: Mapped[str] = mapped_column(String(30), default='open')  # open | answered | superseded
    answer: Mapped[str] = mapped_column(Text, default='')
    evidence_note: Mapped[str] = mapped_column(Text, default='')
    answered_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClaimEvidence(Base):
    __tablename__ = 'claim_evidence'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    claim: Mapped[str] = mapped_column(Text)
    evidence_status: Mapped[str] = mapped_column(String(30), default='unverified')
    source: Mapped[str] = mapped_column(String(255), default='')
    excerpt: Mapped[str] = mapped_column(Text, default='')
    note: Mapped[str] = mapped_column(Text, default='')
    verified_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DecisionRecord(Base):
    __tablename__ = 'decision_records'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    assessment_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision: Mapped[str] = mapped_column(String(40))
    rationale: Mapped[str] = mapped_column(Text)
    conditions: Mapped[str] = mapped_column(Text, default='')
    decided_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScoreCriterion(Base):
    """A human-scored evaluation criterion (scored 1-5 by evaluators), weighted, configured by admins."""
    __tablename__ = 'score_criteria'
    __table_args__ = (UniqueConstraint('institution_id', 'key', name='uq_score_criterion_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='')
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewAssignment(Base):
    """Which evaluators may see and score an application."""
    __tablename__ = 'review_assignments'
    __table_args__ = (UniqueConstraint('application_id', 'reviewer_id', name='uq_assignment'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    assigned_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewScore(Base):
    __tablename__ = 'review_scores'
    __table_args__ = (UniqueConstraint('application_id', 'reviewer_id', 'criterion_id', name='uq_review_score'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    criterion_id: Mapped[str] = mapped_column(ForeignKey('score_criteria.id'))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = 'notifications'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey('applications.id'), nullable=True)
    kind: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Append-only, hash-chained per institution. /api/audit/verify detects edits or deletions."""
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64), default='')
    row_hash: Mapped[str] = mapped_column(String(64), default='')


class KnowledgeItem(Base):
    __tablename__ = 'knowledge_items'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(80), default='policy')
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(40), default='1.0')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FormField(Base):
    """An extra question on the idea form, set up by an administrator (no code change needed)."""
    __tablename__ = 'form_fields'
    __table_args__ = (UniqueConstraint('institution_id', 'key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    key: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(200))
    help: Mapped[str] = mapped_column(Text, default='')
    kind: Mapped[str] = mapped_column(String(20), default='text')  # text|textarea|number|select|yes_no|date|url
    options_json: Mapped[str] = mapped_column(Text, default='[]')
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SiteContent(Base):
    """Editable wording: announcement, welcome text, support details, support categories and response times."""
    __tablename__ = 'site_content'
    __table_args__ = (UniqueConstraint('institution_id', 'key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[str] = mapped_column(Text, default='')
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class FaqItem(Base):
    __tablename__ = 'faq_items'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    category: Mapped[str] = mapped_column(String(80), default='General')
    question: Mapped[str] = mapped_column(String(300))
    answer: Mapped[str] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Contact(Base):
    """Directory of regulators and partner organisations shown on the help page."""
    __tablename__ = 'contacts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(Text, default='')
    phone: Mapped[str] = mapped_column(String(120), default='')
    email: Mapped[str] = mapped_column(String(200), default='')
    website: Mapped[str] = mapped_column(String(200), default='')
    address: Mapped[str] = mapped_column(String(300), default='')
    sort: Mapped[int] = mapped_column(Integer, default=100)
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SupportTicket(Base):
    __tablename__ = 'support_tickets'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    application_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    category: Mapped[str] = mapped_column(String(60))
    subject: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='open', index=True)  # open|answered|closed
    due_at: Mapped[datetime] = mapped_column(DateTime)
    response: Mapped[str] = mapped_column(Text, default='')
    responded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def init_db():
    insp = sa_inspect(engine)
    if insp.has_table('applications'):
        cols = {c['name'] for c in insp.get_columns('applications')}
        if 'programme_id' not in cols or not insp.has_table('assessment_versions'):
            raise RuntimeError(
                'This database was created by an older FundingLens release (<= 1.1). v1.2 changed the schema and '
                'needs a fresh database - see CHANGELOG.md. Pilot data from earlier versions was demo data only.')
        if 'idea_title' not in cols:
            raise RuntimeError(
                'This database was created before the innovation-idea fields were added (1.3). '
                'Start with a fresh database - see CHANGELOG.md.')
    Base.metadata.create_all(engine)
    add_missing_columns('users', USER_COLUMNS_14)
    add_missing_columns('applications', APPLICATION_COLUMNS_15)


APPLICATION_COLUMNS_15 = {'extra_json': ("TEXT DEFAULT '{}'", None)}
USER_COLUMNS_14 = {  # added in 1.4; existing databases get them in place, with safe defaults
    'full_name': ("VARCHAR(200) DEFAULT ''", None), 'email': ('VARCHAR(200)', None), 'phone': ('VARCHAR(40)', None),
    'email_verified': ('BOOLEAN', True), 'pending_email': ('VARCHAR(200)', None), 'otp_login': ('BOOLEAN', False),
    'alert_email': ('BOOLEAN', True), 'alert_sms': ('BOOLEAN', False),
    'gender': ("VARCHAR(20) DEFAULT ''", None), 'birth_year': ('INTEGER', None), 'district': ("VARCHAR(80) DEFAULT ''", None),
}
GENDERS = ('female', 'male', 'other', 'prefer_not')
DISTRICTS = ('Gaborone', 'Francistown', 'Lobatse', 'Selebi-Phikwe', 'Jwaneng', 'Orapa', 'Sowa Town', 'Central', 'Chobe',
             'Ghanzi', 'Kgalagadi', 'Kgatleng', 'Kweneng', 'North-East', 'North-West', 'South-East', 'Southern')


def clean_profile(gender, birth_year, district) -> dict:
    """Validate the optional statistics fields. Empty values mean 'not stated'."""
    out = {}
    if gender is not None:
        if gender not in ('',) + GENDERS:
            raise HTTPException(422, 'Gender must be one of: female, male, other, prefer not to say.')
        out['gender'] = gender
    if birth_year is not None:
        this_year = utcnow().year
        if birth_year and not (this_year - 100 <= birth_year <= this_year - 16):
            raise HTTPException(422, 'Please enter a real year of birth.')
        out['birth_year'] = birth_year or None
    if district is not None:
        if district not in ('',) + DISTRICTS:
            raise HTTPException(422, 'Please choose a district from the list.')
        out['district'] = district
    return out


def add_missing_columns(table: str, columns: dict):
    have = {c['name'] for c in sa_inspect(engine).get_columns(table)}
    missing = [k for k in columns if k not in have]
    if not missing:
        return
    with engine.begin() as conn:
        for name in missing:
            ddl, default = columns[name]
            if default is not None:
                lit = ('TRUE' if default else 'FALSE') if engine.dialect.name == 'postgresql' else ('1' if default else '0')
                ddl = f'{ddl} DEFAULT {lit}'
            conn.execute(sqltext(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
    log.info('Added %s columns: %s', table, ', '.join(missing))


# ----------------------------------------------------------------------------
# Security helpers
# ----------------------------------------------------------------------------
oauth2 = OAuth2PasswordBearer(tokenUrl='/api/token')
_RATE: dict[str, list[float]] = {}


def rate_ok(key: str, limit: int, window: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    if len(_RATE) > 5000:  # purge stale keys so the table cannot grow without bound
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > window]:
            _RATE.pop(k, None)
    bucket = _RATE.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310000)
    return 'pbkdf2_sha256$310000$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b, hash_b = stored.split('$', 3)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt_b), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b))
    except Exception:
        return False


def check_password_policy(password: str, username: str = ''):
    if len(password) < 12:
        raise HTTPException(400, 'Password must contain at least 12 characters.')
    if username and username.lower() in password.lower():
        raise HTTPException(400, 'Password must not contain the username.')


# ---- one-time codes and outgoing messages ---------------------------------------------------
def _code_hash(user_id: int, purpose: str, code: str) -> str:
    return hmac.new(SECRET_KEY.encode(), f'{purpose}:{user_id}:{code}'.encode(), hashlib.sha256).hexdigest()


def issue_code(s: Session, u: User, purpose: str) -> str:
    """A fresh 6-digit code; any earlier unused code for the same purpose stops working."""
    now = utcnow()
    for old in s.scalars(select(OneTimeCode).where(OneTimeCode.user_id == u.id, OneTimeCode.purpose == purpose,
                                                   OneTimeCode.used_at.is_(None))).all():
        old.used_at = now
    code = f'{secrets.randbelow(10 ** 6):06d}'
    s.add(OneTimeCode(id=new_id(), user_id=u.id, purpose=purpose, code_hash=_code_hash(u.id, purpose, code),
                      expires_at=now + timedelta(minutes=CODE_MINUTES[purpose])))
    return code


def check_code(s: Session, u: User, purpose: str, code: str) -> bool:
    """True once for the right, unexpired code. Five wrong tries and the code stops working."""
    row = s.scalar(select(OneTimeCode).where(OneTimeCode.user_id == u.id, OneTimeCode.purpose == purpose,
                                             OneTimeCode.used_at.is_(None)).order_by(OneTimeCode.created_at.desc()))
    if not row or row.expires_at < utcnow() or row.attempts >= CODE_MAX_ATTEMPTS:
        return False
    if not hmac.compare_digest(row.code_hash, _code_hash(u.id, purpose, (code or '').strip())):
        row.attempts += 1
        return False
    row.used_at = utcnow()
    return True


def deliver(s: Session, m: OutboundMessage, real_body: str | None = None):
    """Send one message now. real_body carries text with a code in it, which is never written to the database."""
    text_out = real_body or m.body
    status, detail = (MSG.send_email(m.to_addr, m.subject, text_out) if m.channel == 'email'
                      else MSG.send_sms(m.to_addr, text_out))
    m.status, m.detail, m.sent_at = status, detail[:300], utcnow()
    if status == 'failed':
        log.warning('Message %s to %s failed: %s', m.id, m.channel, detail)


CODE_TEXT = {
    'verify': ('Confirm your email address', 'Welcome to the CEDA Innovation Management Portal. Your verification code is {code}. It expires in 30 minutes.'),
    'reset': ('Reset your password', 'Your password reset code is {code}. It expires in 30 minutes. If you did not ask for this, ignore this message.'),
    'login': ('Your sign-in code', 'Your sign-in code is {code}. It expires in 10 minutes.'),
    'email': ('Confirm your new email address', 'Your code to confirm this email address is {code}. It expires in 30 minutes.'),
}


def send_code(s: Session, u: User, purpose: str, channel: str, to_addr: str) -> dict:
    """Issue a code and send it straight away. Returns what the screen may show about where it went."""
    code = issue_code(s, u, purpose)
    subject, template = CODE_TEXT[purpose]
    m = OutboundMessage(id=new_id(), institution_id=u.institution_id, user_id=u.id, channel=channel, kind=purpose,
                        to_addr=to_addr, subject=subject, body=template.format(code='\u2022' * 6))
    s.add(m)
    deliver(s, m, real_body=template.format(code=code))
    out = {'sent_to': MSG.mask_email(to_addr) if channel == 'email' else to_addr[:-4] + '****', 'delivery': m.status}
    if DEMO_SHOW_CODES and m.status != 'sent':
        out['demo_code'] = code
    return out


def queue_alert(s: Session, u: User, subject: str, text_body: str):
    """Alerts the user subscribed to, by email and/or SMS. The background sender delivers them."""
    link = f'\n\nOpen the portal: {PORTAL_URL}' if PORTAL_URL else ''
    if u.alert_email and u.email and u.email_verified:
        s.add(OutboundMessage(id=new_id(), institution_id=u.institution_id, user_id=u.id, channel='email', kind='alert',
                              to_addr=u.email, subject=subject, body=text_body + link))
    if u.alert_sms and u.phone:
        s.add(OutboundMessage(id=new_id(), institution_id=u.institution_id, user_id=u.id, channel='sms', kind='alert',
                              to_addr=u.phone, subject=subject, body=text_body[:300]))


def dispatch_outbox(limit: int = 50) -> int:
    """Deliver queued alerts, oldest first. Called by the background sender, and directly in tests."""
    s = SessionLocal()
    try:
        rows = s.scalars(select(OutboundMessage).where(OutboundMessage.status == 'queued')
                         .order_by(OutboundMessage.created_at).limit(limit)).all()
        for m in rows:
            deliver(s, m)
            s.commit()
        return len(rows)
    finally:
        s.close()


_DISPATCHER_STARTED = False


def start_dispatcher():
    global _DISPATCHER_STARTED
    if _DISPATCHER_STARTED or ENVIRONMENT == 'test' or os.getenv('ALERT_DISPATCH', 'on') == 'off':
        return
    _DISPATCHER_STARTED = True

    def loop():
        while True:
            try:
                dispatch_outbox()
            except Exception:  # never let one bad message stop alerts for everyone
                log.exception('Alert dispatch failed')
            time.sleep(5)
    threading.Thread(target=loop, name='alert-dispatcher', daemon=True).start()


def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def token_for(u: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub': str(u.id), 'tv': u.token_version, 'iat': now,
                       'exp': now + timedelta(minutes=ACCESS_MINUTES)}, SECRET_KEY, algorithm=ALGORITHM)


def current_user(token: str = Depends(oauth2), s: Session = Depends(db)) -> User:
    try:
        p = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(p['sub'])
    except Exception:
        raise HTTPException(401, 'Invalid or expired token')
    u = s.get(User, uid)
    inst = s.get(Institution, u.institution_id) if u else None
    if not u or not u.active or not inst or not inst.active or p.get('tv') != u.token_version:
        raise HTTPException(401, 'Session is no longer valid')
    return u


def require_roles(*roles):
    def dep(u: User = Depends(current_user)) -> User:
        if u.role not in roles:
            raise HTTPException(403, 'Insufficient permissions')
        return u
    return dep


def audit(s: Session, institution_id: str, user_id, action: str, etype: str, eid, detail: str = ''):
    """Add a hash-chained audit row to the *current* transaction. The caller commits once,
    so the action and its audit record succeed or fail together."""
    if engine.dialect.name == 'postgresql':
        s.execute(sqltext('SELECT pg_advisory_xact_lock(hashtext(:k))'), {'k': institution_id})
    s.flush()
    last = s.scalar(select(AuditLog).where(AuditLog.institution_id == institution_id).order_by(AuditLog.id.desc()).limit(1))
    prev = last.row_hash if last else ''
    ts = utcnow()
    payload = json.dumps([institution_id, user_id, action, etype, str(eid), detail, ts.isoformat()], sort_keys=True)
    row = AuditLog(institution_id=institution_id, user_id=user_id, action=action, entity_type=etype,
                   entity_id=str(eid), detail=detail, created_at=ts, prev_hash=prev,
                   row_hash=hashlib.sha256((prev + payload).encode()).hexdigest())
    s.add(row)
    s.flush()


def verify_audit_chain(rows: list[AuditLog]) -> dict:
    prev = ''
    for r in rows:
        payload = json.dumps([r.institution_id, r.user_id, r.action, r.entity_type, r.entity_id, r.detail,
                              r.created_at.isoformat()], sort_keys=True)
        if r.prev_hash != prev or r.row_hash != hashlib.sha256((prev + payload).encode()).hexdigest():
            return {'ok': False, 'first_bad_id': r.id, 'checked': len(rows)}
        prev = r.row_hash
    return {'ok': True, 'first_bad_id': None, 'checked': len(rows)}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


# ----------------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------------
def amount(default=0.0):
    return Field(default=default, ge=0, le=1e12, allow_inf_nan=False)


class ApplicationCreate(BaseModel):
    applicant_name: str = Field(min_length=2, max_length=200)
    funding_request: float = amount()
    owner_contribution: float = amount()
    programme_id: str | None = None
    idea_title: str = Field(default='', max_length=200)
    problem: str = Field(default='', max_length=5000)
    solution: str = Field(default='', max_length=5000)
    target_users: str = Field(default='', max_length=3000)
    sector: str = Field(default='', max_length=80)
    stage: str = Field(default='', max_length=80)
    answers: dict = Field(default_factory=dict)


IDEA_REQUIRED = (('idea_title', 'Idea title'), ('problem', 'Problem'), ('solution', 'Proposed solution'),
                 ('target_users', 'Target users'), ('sector', 'Sector'), ('stage', 'Stage'))


def missing_idea_fields(obj) -> list[str]:
    return [label for key, label in IDEA_REQUIRED if not str(getattr(obj, key, '') or '').strip()]


class IdeaUpdate(BaseModel):
    idea_title: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=5000)
    solution: str = Field(min_length=1, max_length=5000)
    target_users: str = Field(min_length=1, max_length=3000)
    sector: str = Field(min_length=1, max_length=80)
    stage: str = Field(min_length=1, max_length=80)
    answers: dict | None = None


IDEA_TEXT_FIELDS = ('idea_title', 'problem', 'solution', 'target_users', 'sector', 'stage')


class AssignmentRequest(BaseModel):
    reviewer_ids: list[int] = Field(default_factory=list, max_length=10)


class ScoreItem(BaseModel):
    criterion_id: str
    score: int = Field(ge=1, le=5)
    comment: str = Field(default='', max_length=2000)


class ScoreRequest(BaseModel):
    scores: list[ScoreItem] = Field(min_length=1, max_length=30)


class ScoreCriterionRequest(BaseModel):
    key: str = Field(pattern=r'^[a-z0-9_]{2,60}$')
    label: str = Field(min_length=2, max_length=200)
    description: str = Field(default='', max_length=2000)
    weight: float = Field(default=1.0, gt=0, le=100)
    enabled: bool = True


class FinancialsOverride(BaseModel):
    revenue: float | None = Field(default=None, ge=0, le=1e13, allow_inf_nan=False)
    expenses: float | None = Field(default=None, ge=0, le=1e13, allow_inf_nan=False)


class DDAnswer(BaseModel):
    answer: str = Field(default='', max_length=5000)
    evidence_note: str = Field(default='', max_length=2000)


class DecisionRequest(BaseModel):
    decision: str
    rationale: str = Field(min_length=10, max_length=5000)
    conditions: str = Field(default='', max_length=3000)


class ClaimEvidenceRequest(BaseModel):
    claim: str = Field(min_length=3, max_length=2000)
    evidence_status: str = 'verified'
    source: str = Field(default='', max_length=255)
    excerpt: str = Field(default='', max_length=3000)
    note: str = Field(default='', max_length=2000)


class KnowledgeRequest(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    category: str = Field(default='policy', max_length=80)
    content: str = Field(min_length=20, max_length=20000)
    version: str = Field(default='1.0', max_length=40)
    active: bool = True


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=60, pattern=r'^[A-Za-z0-9_.-]+$')
    password: str
    role: str
    full_name: str = Field(default='', max_length=200)
    email: str = Field(default='', max_length=200)
    phone: str = Field(default='', max_length=40)


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    username: str = Field(min_length=3, max_length=60, pattern=r'^[A-Za-z0-9_.-]+$')
    email: str = Field(min_length=5, max_length=200)
    phone: str = Field(default='', max_length=40)
    password: str = Field(max_length=200)
    accept_privacy: bool
    gender: str = Field(default='', max_length=20)
    birth_year: int | None = None
    district: str = Field(default='', max_length=80)


class CodeRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=200)   # username or email
    code: str = Field(min_length=4, max_length=10)


class IdentifierRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=200)


class ResetRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=4, max_length=10)
    new_password: str = Field(max_length=200)


class PreferencesUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    alert_email: bool | None = None
    alert_sms: bool | None = None
    otp_login: bool | None = None
    gender: str | None = Field(default=None, max_length=20)
    birth_year: int | None = None
    district: str | None = Field(default=None, max_length=80)


class EmailCode(BaseModel):
    code: str = Field(min_length=4, max_length=10)


class UserUpdate(BaseModel):
    active: bool | None = None
    role: str | None = None
    password: str | None = None


class ProgramRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    code: str = Field(min_length=2, max_length=80)
    description: str = Field(default='', max_length=2000)
    min_request: float = amount()
    max_request: float = amount()
    min_owner_contribution_pct: float = Field(default=0, ge=0, le=100)
    enabled: bool = True


class InstitutionSettings(BaseModel):
    ai_enabled: bool


class CriterionRequest(BaseModel):
    key: str = Field(pattern=r'^[a-z0-9_]{2,60}$')
    label: str = Field(min_length=2, max_length=200)
    description: str = Field(default='', max_length=2000)
    kind: str
    programme_id: str | None = None
    metric: str | None = None
    comparator: str | None = None
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    mandatory: bool = False
    weight: float = Field(default=1.0, ge=0, le=10)
    enabled: bool = True


def validate_criterion(req: CriterionRequest):
    if req.kind not in A.CRITERION_KINDS:
        raise HTTPException(422, f'kind must be one of {list(A.CRITERION_KINDS)}')
    if req.kind == 'numeric':
        if req.metric not in A.METRIC_KEYS:
            raise HTTPException(422, f'metric must be one of {list(A.METRIC_KEYS)}')
        if req.comparator not in A.COMPARATORS:
            raise HTTPException(422, f'comparator must be one of {list(A.COMPARATORS)}')
        if req.threshold is None:
            raise HTTPException(422, 'threshold is required for numeric criteria')
    if req.kind == 'evidence':
        kws = [k.strip() for k in req.keywords if k.strip()]
        if not kws or any(len(k) > 80 for k in kws):
            raise HTTPException(422, 'evidence criteria need 1-20 keywords of at most 80 characters')
    if req.kind == 'qualitative' and req.weight:
        raise HTTPException(422, 'qualitative criteria are officer-judged and must have weight 0')


# ----------------------------------------------------------------------------
# Workflow
# ----------------------------------------------------------------------------
EDITABLE_STATUSES = {'draft', 'under_review', 'request_information'}
DECISION_RULES = {  # decision -> (roles allowed, statuses it may be taken from)
    'request_information': ({'officer', 'committee', 'admin'}, {'under_review', 'refer_committee'}),
    'refer_committee': ({'officer', 'admin'}, {'under_review'}),
    'approve': ({'committee', 'admin'}, {'refer_committee'}),
    'decline': ({'committee', 'admin'}, {'refer_committee'}),
}


def allowed_decisions(a: Application, u: User) -> list[str]:
    if not a.latest_version:
        return []
    out = []
    for d, (roles, statuses) in DECISION_RULES.items():
        if u.role in roles and a.status in statuses:
            if d in ('approve', 'decline') and a.referred_by == u.id:
                continue  # separation of duties
            out.append(d)
    return out


def is_assigned(s: Session, app_id: str, user_id: int) -> bool:
    return s.scalar(select(func.count()).select_from(ReviewAssignment).where(
        ReviewAssignment.application_id == app_id, ReviewAssignment.reviewer_id == user_id)) > 0


def get_app_or_404(s: Session, app_id: str, u: User) -> Application:
    """Visibility: applicants see their own; evaluators (officers) see what they created or were assigned;
    adjudicators (committee) and admins see everything in their institution."""
    a = s.get(Application, app_id)
    if not a or a.institution_id != u.institution_id or (u.role == 'applicant' and a.created_by != u.id):
        raise HTTPException(404, 'Application not found')
    if u.role == 'officer' and a.created_by != u.id and not is_assigned(s, a.id, u.id):
        raise HTTPException(404, 'Application not found')
    return a


def jl(text, default=None):
    try:
        return json.loads(text) if text else default
    except Exception:
        return default


def notify(s: Session, inst_id: str, user_ids, app: Application, kind: str, message: str):
    for uid in set(user_ids):
        s.add(Notification(institution_id=inst_id, user_id=uid, application_id=app.id, kind=kind, message=message))
        u = s.get(User, uid)
        if u and u.active:
            queue_alert(s, u, 'Update on ' + (app.idea_title or app.applicant_name or 'your idea')[:150], message)


def staff_ids(s: Session, inst_id: str, roles=STAFF, exclude=None):
    rows = s.scalars(select(User.id).where(User.institution_id == inst_id, User.active == True,  # noqa: E712
                                           User.role.in_(roles))).all()
    return [i for i in rows if i != exclude]


# ----------------------------------------------------------------------------
# Rubric, knowledge, uploads, AI
# ----------------------------------------------------------------------------
def criterion_dict(c: RubricCriterion) -> dict:
    return {'id': c.id, 'key': c.key, 'label': c.label, 'description': c.description, 'kind': c.kind,
            'programme_id': c.programme_id, 'metric': c.metric, 'comparator': c.comparator,
            'threshold': c.threshold, 'keywords': jl(c.keywords_json, []), 'mandatory': c.mandatory,
            'weight': c.weight, 'enabled': c.enabled}


def load_criteria(s: Session, inst_id: str, programme_id: str | None) -> list[dict]:
    cond = RubricCriterion.programme_id.is_(None)
    if programme_id:
        cond = or_(cond, RubricCriterion.programme_id == programme_id)
    rows = s.scalars(select(RubricCriterion).where(RubricCriterion.institution_id == inst_id,
                                                   RubricCriterion.enabled == True, cond)  # noqa: E712
                     .order_by(RubricCriterion.created_at)).all()
    by_key: dict[str, RubricCriterion] = {}
    for r in rows:  # a programme-specific criterion overrides the institution-wide one with the same key
        if r.key not in by_key or r.programme_id:
            by_key[r.key] = r
    return [criterion_dict(r) for r in by_key.values()]


def retrieve_knowledge(s: Session, inst_id: str, text: str, limit: int = 5) -> list[dict]:
    rows = s.scalars(select(KnowledgeItem).where(KnowledgeItem.institution_id == inst_id,
                                                 KnowledgeItem.active == True)).all()  # noqa: E712
    terms = set(re.findall(r'[a-zA-Z]{4,}', text.lower()))
    scored = []
    for x in rows:
        score = len(terms & set(re.findall(r'[a-zA-Z]{4,}', (x.title + ' ' + x.content).lower())))
        if score:
            scored.append((score, x))
    scored.sort(key=lambda z: z[0], reverse=True)
    return [{'title': x.title, 'category': x.category, 'version': x.version, 'content': x.content, 'score': sc}
            for sc, x in scored[:limit]]


def validate_upload(data: bytes, suffix: str):
    if suffix not in {'.txt', '.md', '.csv', '.docx', '.pdf'}:
        raise HTTPException(415, 'Supported files: PDF, DOCX, TXT, MD, CSV')
    if not data:
        raise HTTPException(400, 'The uploaded file is empty.')
    if suffix == '.pdf' and not data.startswith(b'%PDF'):
        raise HTTPException(415, 'File content does not match a PDF.')
    if suffix == '.docx':
        if not data.startswith(b'PK'):
            raise HTTPException(415, 'File content does not match a DOCX.')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 100_000_000 or len(z.infolist()) > 2000:
                    raise HTTPException(413, 'The DOCX expands to an unsafe size.')
        except zipfile.BadZipFile:
            raise HTTPException(415, 'File content does not match a DOCX.')


def extract_doc(data: bytes, suffix: str) -> str:
    try:
        if suffix in {'.txt', '.md', '.csv'}:
            return data.decode('utf-8', errors='ignore')
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as t:
            t.write(data)
            path = t.name
        try:
            if suffix == '.docx':
                from docx import Document as DocxDocument
                d = DocxDocument(path)
                out = '\n'.join(p.text for p in d.paragraphs)
                for table in d.tables:
                    for row in table.rows:
                        out += '\n' + ' | '.join(c.text for c in row.cells)
                return out
            from pypdf import PdfReader
            reader = PdfReader(path)
            return '\n'.join((p.extract_text() or '') for p in reader.pages[:300])
        finally:
            os.unlink(path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, 'The document could not be read. It may be corrupt or password-protected.')


def run_ai(institution: Institution, text: str, applicant: str, det: dict, criteria: list[dict], knowledge: list[dict]):
    """Returns (sanitised_ai_output | None, meta, raw_json | None). Never raises."""
    key, model = os.getenv('OPENAI_API_KEY'), os.getenv('OPENAI_MODEL')
    base = {'prompt_version': A.PROMPT_VERSION}
    if not institution.ai_enabled:
        return None, {**base, 'mode': 'rules_only', 'model': None,
                      'message': 'AI assistance is switched off for this institution; rules and rubric only.'}, None
    if not key or not model or OpenAI is None:
        return None, {**base, 'mode': 'rules_only', 'model': None,
                      'message': 'AI is enabled but OPENAI_API_KEY / OPENAI_MODEL are not configured; rules and rubric only.'}, None
    payload, sha, truncated = A.build_ai_input(text, applicant, det['metrics'], criteria, knowledge)
    meta = {**base, 'model': model, 'input_sha256': sha, 'truncated': truncated, 'redacted': True}
    try:
        client = OpenAI(api_key=key, timeout=float(os.getenv('OPENAI_TIMEOUT', '45')), max_retries=1)
        resp = client.responses.create(
            model=model, instructions=A.AI_INSTRUCTIONS, input=payload,
            text={'format': {'type': 'json_schema', 'name': 'fundinglens_assessment', 'strict': True, 'schema': A.AI_SCHEMA}})
        raw = json.loads(resp.output_text)
        out = A.sanitize_ai_output(raw, text, {c['key'] for c in criteria})
        return out, {**meta, 'mode': 'llm', 'message': 'AI-assisted commentary added. Human verification required.'}, json.dumps(out)
    except Exception as e:  # network, quota, schema, parsing - always degrade to rules
        log.warning('AI call failed: %s', type(e).__name__)
        return None, {**meta, 'mode': 'rules_only', 'message': f'AI call failed ({type(e).__name__}); rules and rubric only.'}, None


def store_assessment(s: Session, a: Application, u: User, det: dict, ai_out, ai_meta, ai_raw, doc_id, trigger: str) -> dict:
    result = A.build_result(det, ai_out, ai_meta)
    version = (a.latest_version or 0) + 1
    s.add(AssessmentVersion(id=new_id(), institution_id=a.institution_id, application_id=a.id, version=version,
                            document_id=doc_id, trigger=trigger, result_json=json.dumps(result), ai_output_json=ai_raw,
                            ai_mode=ai_meta.get('mode', 'rules_only'), ai_model=ai_meta.get('model'),
                            prompt_version=ai_meta.get('prompt_version'), input_sha256=ai_meta.get('input_sha256'),
                            created_by=u.id))
    sev = {x: sum(1 for r in result['risks'] if r.get('severity') == x) for x in A.RISK_SEVERITIES}
    a.latest_version = version
    a.coverage_pct = result['rubric']['coverage_pct']
    a.high_risks, a.medium_risks, a.low_risks = sev['high'], sev['medium'], sev['low']
    result['assessment_version'] = version
    sync_dd_questions(s, a, result)
    return result


def sync_dd_questions(s: Session, a: Application, result: dict):
    """Keep answered questions; add new ones; mark open questions that no longer apply as superseded."""
    existing = {q.qkey: q for q in s.scalars(select(DueDiligenceQuestion).where(
        DueDiligenceQuestion.application_id == a.id, DueDiligenceQuestion.institution_id == a.institution_id)).all()}
    wanted = set()
    for q in A.build_dd_questions(result):
        k = hashlib.sha1(A.question_key(q['question']).encode()).hexdigest()
        wanted.add(k)
        row = existing.get(k)
        if row is None:
            s.add(DueDiligenceQuestion(id=new_id(), institution_id=a.institution_id, application_id=a.id, qkey=k, **q))
        elif row.status == 'superseded':
            row.status = 'answered' if row.answer.strip() else 'open'
    for k, row in existing.items():
        if k not in wanted and row.status == 'open':
            row.status = 'superseded'


def current_overrides(a: Application) -> dict:
    return {'revenue': a.revenue_override, 'expenses': a.expenses_override}


def process_upload(s: Session, inst: Institution, a: Application, u: User, data: bytes, filename: str,
                   content_type: str) -> dict:
    suffix = Path(filename or '').suffix.lower()
    validate_upload(data, suffix)
    text = extract_doc(data, suffix)
    if len(text.strip()) < 50:
        raise HTTPException(422, 'No readable text was found in this file. Scanned PDFs need OCR - please upload a '
                                 'text-based PDF or a DOCX.')
    prior = s.scalar(select(Document).where(Document.application_id == a.id).order_by(Document.version.desc()).limit(1))
    version = (prior.version + 1) if prior else 1
    stored = f'{a.id}_v{version}_{uuid.uuid4().hex}{suffix}'
    path = DOCS / stored
    path.write_bytes(data)
    try:
        doc = Document(id=new_id(), institution_id=a.institution_id, application_id=a.id,
                       original_name=(filename or stored)[:255], storage_name=stored,
                       content_type=(content_type or 'application/octet-stream')[:120], size_bytes=len(data),
                       sha256=hashlib.sha256(data).hexdigest(), version=version, created_by=u.id)
        s.add(doc)
        criteria = load_criteria(s, a.institution_id, a.programme_id)
        det = A.analyse_document(text, a.funding_request, a.owner_contribution, criteria, current_overrides(a))
        knowledge = retrieve_knowledge(s, a.institution_id, text[:4000])
        ai_out, ai_meta, ai_raw = run_ai(inst, text, a.applicant_name, det, criteria, knowledge)
        s.flush()
        result = store_assessment(s, a, u, det, ai_out, ai_meta, ai_raw, doc.id, 'upload')
        result['source_file'], result['document_version'] = doc.original_name, version
        if a.status in ('draft', 'request_information'):
            a.status = 'under_review'
        audit(s, a.institution_id, u.id, 'analyse', 'application', a.id,
              f'Uploaded {doc.original_name} (document v{version}); assessment v{result["assessment_version"]}; ai={ai_meta["mode"]}')
        s.commit()
        return result
    except Exception:
        s.rollback()
        path.unlink(missing_ok=True)
        raise


# ----------------------------------------------------------------------------
# Seed data
# ----------------------------------------------------------------------------
DEFAULT_SCORE_CRITERIA = (  # key, label, description, weight (weights are relative)
    ('innovation', 'Innovation and originality', 'How new or different is the idea compared with what exists?', 25),
    ('feasibility', 'Feasibility', 'Can the idea realistically be built and delivered with the resources described?', 25),
    ('impact', 'Market and social impact', 'Size of the need, likely users, jobs and benefit to Botswana.', 25),
    ('team', 'Team capability', 'Does the team have the skills to deliver?', 15),
    ('alignment', 'Alignment with CEDA priorities', 'Fit with digital advancement and sustainable development goals.', 10),
)


def seed():
    s = SessionLocal()
    try:
        code = os.getenv('INSTITUTION_CODE', 'PILOT')
        inst = s.scalar(select(Institution).where(Institution.code == code))
        if not inst:
            inst = Institution(id=new_id(), name=os.getenv('DEFAULT_INSTITUTION_NAME', 'FundingLens Pilot Institution'), code=code)
            s.add(inst)
            s.flush()
        elif os.getenv('DEFAULT_INSTITUTION_NAME') and inst.name != os.getenv('DEFAULT_INSTITUTION_NAME'):
            inst.name = os.getenv('DEFAULT_INSTITUTION_NAME')  # keep the deployed name in step with configuration
        admin = s.scalar(select(User).where(User.username == 'admin'))
        if not admin:
            admin = User(username='admin', password_hash=hash_password(ADMIN_PASSWORD), role='admin', institution_id=inst.id)
            s.add(admin)
            s.flush()
        prog = s.scalar(select(FundingProgram).where(FundingProgram.institution_id == inst.id))
        if not prog:
            prog = FundingProgram(id=new_id(), institution_id=inst.id,
                                  name=os.getenv('DEFAULT_PROGRAMME_NAME', 'Innovation Challenge 2026'), code='INNOVATION',
                                  description='Configurable call for innovative ideas.',
                                  min_request=0, max_request=5_000_000, min_owner_contribution_pct=0)
            s.add(prog)
        if not s.scalar(select(ScoreCriterion).where(ScoreCriterion.institution_id == inst.id)):
            for key, label, desc, weight in DEFAULT_SCORE_CRITERIA:
                s.add(ScoreCriterion(id=new_id(), institution_id=inst.id, key=key, label=label, description=desc, weight=weight))
        if not s.scalar(select(RubricCriterion).where(RubricCriterion.institution_id == inst.id)):
            for c in A.DEFAULT_RUBRIC:
                s.add(RubricCriterion(id=new_id(), institution_id=inst.id, key=c['key'], label=c['label'],
                                      description=c.get('description', ''), kind=c['kind'], metric=c.get('metric'),
                                      comparator=c.get('comparator'), threshold=c.get('threshold'),
                                      keywords_json=json.dumps(c.get('keywords', [])), mandatory=c['mandatory'],
                                      weight=c['weight']))
        if not s.scalar(select(KnowledgeItem).where(KnowledgeItem.institution_id == inst.id)):
            s.add(KnowledgeItem(id=new_id(), institution_id=inst.id, created_by=admin.id,
                                title='Funding Assessment Governance Policy', category='policy',
                                content='AI analysis is advisory. Eligibility checks and rubric results support officer review. '
                                        'Financial figures are calculated by deterministic code and extracted figures must be '
                                        'confirmed by an officer. Final approval or decline requires an authorised human '
                                        'decision-maker who did not refer the application.'))
        seed_content(s, inst)
        s.commit()
        if os.getenv('SEED_DEMO', '').lower() in ('1', 'true', 'yes'):
            seed_demo(s, inst, prog)
    finally:
        s.close()


def seed_demo(s: Session, inst: Institution, prog: FundingProgram):
    pw = os.getenv('DEMO_PASSWORD', '')
    if len(pw) < 12:
        log.warning('SEED_DEMO is set but DEMO_PASSWORD is missing or shorter than 12 characters - demo users not created.')
        return
    for name, role in (('officer1', 'officer'), ('committee1', 'committee'), ('applicant1', 'applicant')):
        if not s.scalar(select(User).where(User.username == name)):
            s.add(User(username=name, password_hash=hash_password(pw), role=role, institution_id=inst.id))
    s.commit()
    app_user = s.scalar(select(User).where(User.username == 'applicant1'))
    if s.scalar(select(Application).where(Application.institution_id == inst.id, Application.applicant_name.like('%(Demo)'))):
        return
    plan = ROOT / 'demo_data' / 'business_plan_demo.txt'
    if not plan.exists():
        return
    a = Application(id=new_id(), institution_id=inst.id, programme_id=prog.id, applicant_name='Example Agri Processing (Demo)',
                    funding_request=500000, owner_contribution=100000, created_by=app_user.id,
                    idea_title='Solar-powered sorghum milling for rural cooperatives',
                    problem='Rural farmers travel long distances and pay high diesel costs to mill sorghum.',
                    solution='Small solar milling units owned and run by village cooperatives, with mobile payments.',
                    target_users='Smallholder farmers and village cooperatives', sector='Agriculture', stage='Prototype')
    s.add(a)
    s.commit()
    process_upload(s, inst, a, app_user, plan.read_bytes(), 'business_plan_demo.txt', 'text/plain')
    officer = s.scalar(select(User).where(User.username == 'officer1'))
    admin = s.scalar(select(User).where(User.username == 'admin'))
    if officer and admin:
        s.add(ReviewAssignment(id=new_id(), institution_id=inst.id, application_id=a.id, reviewer_id=officer.id,
                               assigned_by=admin.id))
        s.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed()
    start_dispatcher()
    yield


# ----------------------------------------------------------------------------
# App and middleware
# ----------------------------------------------------------------------------
app = FastAPI(title='FundingLens AI Institutional Platform', version=VERSION, lifespan=lifespan)
if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=['GET', 'POST', 'PUT', 'PATCH'],
                       allow_headers=['Authorization', 'Content-Type'], allow_credentials=False)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware('http')
async def security_middleware(request: Request, call_next):
    cl = request.headers.get('content-length')
    if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES + 1_000_000:
        return JSONResponse({'detail': 'Request body too large.'}, status_code=413)
    response = await call_next(request)
    h = response.headers
    h['X-Content-Type-Options'] = 'nosniff'
    h['X-Frame-Options'] = 'DENY'
    h['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    h['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    h['Content-Security-Policy'] = CSP
    if IS_PROD:
        h['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    if request.url.path.startswith('/api/'):
        h['Cache-Control'] = 'no-store'
    else:
        # the page, styles and script: browsers must check for a newer copy on every visit (cheap, thanks to ETags),
        # so a phone never mixes a new page with an old stylesheet after a release
        h['Cache-Control'] = 'no-cache'
    return response


# ----------------------------------------------------------------------------
# Health, auth, users, settings
# ----------------------------------------------------------------------------
@app.get('/api/health')
def health():
    return {'status': 'ok', 'service': 'FundingLens AI', 'version': VERSION}


@app.get('/api/health/ready')
def readiness(s: Session = Depends(db)):
    checks = {'database': False, 'storage': DOCS.exists()}
    try:
        s.execute(sqltext('SELECT 1'))
        checks['database'] = True
    except Exception:
        pass
    ok = all(checks.values())
    return JSONResponse({'status': 'ready' if ok else 'degraded', 'version': VERSION, 'checks': checks,
                         'environment': ENVIRONMENT}, status_code=200 if ok else 503)


@app.post('/api/token')
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), s: Session = Depends(db)):
    ip = client_ip(request)
    if not rate_ok(f'login:{ip}:{form.username.lower()}', LOGIN_LIMIT, 300) or not rate_ok(f'loginip:{ip}', LOGIN_IP_LIMIT, 300):
        raise HTTPException(429, 'Too many login attempts; try again later.')
    u = find_user(s, form.username)
    if not u or not u.active or not verify_password(form.password, u.password_hash):
        if u:
            audit(s, u.institution_id, u.id, 'login_failed', 'user', u.id, f'ip={ip}')
            s.commit()
        raise HTTPException(401, 'Incorrect username or password')
    if not u.email_verified:
        sent = {}
        if u.email and rate_ok(f'resend:{u.id}', 3, 600):
            sent = send_code(s, u, 'verify', 'email', u.email)
            s.commit()
        return JSONResponse(status_code=403, content={'detail': 'Please confirm your email address first.',
                                                      'verification_required': True, 'username': u.username, **sent})
    if u.otp_login and u.email:
        sent = send_code(s, u, 'login', 'email', u.email)
        audit(s, u.institution_id, u.id, 'login_code_sent', 'user', u.id, f'ip={ip}')
        s.commit()
        return {'otp_required': True, 'username': u.username, **sent}
    audit(s, u.institution_id, u.id, 'login', 'user', u.id, f'ip={ip}')
    s.commit()
    return {'access_token': token_for(u), 'token_type': 'bearer', 'role': u.role, 'username': u.username}


def find_user(s: Session, identifier: str) -> User | None:
    """By username, or by email address when it looks like one."""
    ident = (identifier or '').strip()
    u = s.scalar(select(User).where(User.username == ident))
    if not u and '@' in ident:
        u = s.scalar(select(User).where(func.lower(User.email) == ident.lower()))
    return u


def default_institution(s: Session) -> Institution:
    inst = s.scalar(select(Institution).where(Institution.code == os.getenv('INSTITUTION_CODE', 'PILOT')))
    return inst or s.scalar(select(Institution).order_by(Institution.created_at))


def email_taken(s: Session, email: str, except_id: int | None = None) -> bool:
    q = select(User.id).where(or_(func.lower(User.email) == email.lower(), func.lower(User.pending_email) == email.lower()))
    return any(i != except_id for i in s.scalars(q).all())


def clean_contact(email: str, phone: str) -> tuple[str, str]:
    email, phone = (email or '').strip().lower(), MSG.normalise_phone(phone or '')
    if email and not MSG.valid_email(email):
        raise HTTPException(422, 'Please enter a valid email address.')
    if phone and not MSG.valid_phone(phone):
        raise HTTPException(422, 'Please enter a valid phone number, e.g. 71 234 567.')
    return email, phone


@app.post('/api/auth/register')
def register(req: RegisterRequest, request: Request, s: Session = Depends(db)):
    """Anyone can create a submitter account. It works once the emailed code is confirmed."""
    if not SELF_REGISTRATION:
        raise HTTPException(403, 'Self-registration is switched off. Ask the portal administrator for an account.')
    if not rate_ok(f'register:{client_ip(request)}', REGISTER_LIMIT, 3600):
        raise HTTPException(429, 'Too many sign-ups from this connection. Try again later.')
    if not req.accept_privacy:
        raise HTTPException(422, 'Please accept the privacy notice to continue.')
    email, phone = clean_contact(req.email, req.phone)
    if not email:
        raise HTTPException(422, 'Please enter a valid email address.')
    check_password_policy(req.password, req.username)
    if s.scalar(select(User).where(func.lower(User.username) == req.username.lower())):
        raise HTTPException(409, 'That username is already taken.')
    if email_taken(s, email):
        raise HTTPException(409, 'An account with that email address already exists. Try signing in, or reset your password.')
    inst = default_institution(s)
    u = User(username=req.username, password_hash=hash_password(req.password), role='applicant', institution_id=inst.id,
             full_name=req.full_name.strip(), email=email, phone=phone or None, email_verified=False,
             **clean_profile(req.gender, req.birth_year, req.district))
    s.add(u)
    s.flush()
    audit(s, inst.id, u.id, 'register', 'user', u.id, json.dumps({'username': u.username}))
    sent = send_code(s, u, 'verify', 'email', email)
    s.commit()
    return {'status': 'verification_sent', 'username': u.username, **sent}


@app.post('/api/auth/verify')
def verify_email(req: CodeRequest, request: Request, s: Session = Depends(db)):
    if not rate_ok(f'code:{client_ip(request)}', 20, 900):
        raise HTTPException(429, 'Too many attempts. Try again later.')
    u = find_user(s, req.identifier)
    if not u or not u.active or u.email_verified or not check_code(s, u, 'verify', req.code):
        s.commit()  # keep the failed-attempt count
        raise HTTPException(400, 'That code is not right, or it has expired. Ask for a new one.')
    u.email_verified = True
    audit(s, u.institution_id, u.id, 'verify_email', 'user', u.id)
    s.commit()
    return {'access_token': token_for(u), 'token_type': 'bearer', 'role': u.role, 'username': u.username}


@app.post('/api/auth/resend')
def resend_code(req: IdentifierRequest, request: Request, s: Session = Depends(db)):
    u = find_user(s, req.identifier)
    if u and u.active and not u.email_verified and u.email and rate_ok(f'resend:{u.id}', 3, 600) \
            and rate_ok(f'resendip:{client_ip(request)}', 10, 900):
        sent = send_code(s, u, 'verify', 'email', u.email)
        s.commit()
        return {'status': 'sent', **sent}
    return {'status': 'sent'}


@app.post('/api/auth/forgot')
def forgot_password(req: IdentifierRequest, request: Request, s: Session = Depends(db)):
    """Same answer whether or not the account exists, so the form cannot be used to find out who has one."""
    ip = client_ip(request)
    if not rate_ok(f'forgotip:{ip}', 10, 900):
        raise HTTPException(429, 'Too many requests. Try again later.')
    u = find_user(s, req.identifier)
    out = {'status': 'sent_if_exists'}
    if u and u.active and rate_ok(f'forgot:{u.id}', 3, 900):
        if u.email:
            out.update(send_code(s, u, 'reset', 'email', u.email))
        elif u.phone:
            out.update(send_code(s, u, 'reset', 'sms', u.phone))
        audit(s, u.institution_id, u.id, 'password_reset_requested', 'user', u.id, f'ip={ip}')
        s.commit()
    out.pop('delivery', None)
    return out


@app.post('/api/auth/reset')
def reset_password(req: ResetRequest, request: Request, s: Session = Depends(db)):
    if not rate_ok(f'code:{client_ip(request)}', 20, 900):
        raise HTTPException(429, 'Too many attempts. Try again later.')
    u = find_user(s, req.identifier)
    if not u or not u.active:
        raise HTTPException(400, 'That code is not right, or it has expired. Ask for a new one.')
    check_password_policy(req.new_password, u.username)
    if not check_code(s, u, 'reset', req.code):
        s.commit()
        raise HTTPException(400, 'That code is not right, or it has expired. Ask for a new one.')
    u.password_hash = hash_password(req.new_password)
    u.token_version += 1  # signs out every other session
    if u.email:
        u.email_verified = True  # they proved they can read that inbox
    audit(s, u.institution_id, u.id, 'password_reset', 'user', u.id)
    s.commit()
    return {'status': 'reset'}


@app.post('/api/auth/login-code')
def login_with_code(req: CodeRequest, request: Request, s: Session = Depends(db)):
    if not rate_ok(f'code:{client_ip(request)}', 20, 900):
        raise HTTPException(429, 'Too many attempts. Try again later.')
    u = find_user(s, req.identifier)
    if not u or not u.active or not check_code(s, u, 'login', req.code):
        s.commit()
        raise HTTPException(400, 'That code is not right, or it has expired. Sign in again for a new one.')
    audit(s, u.institution_id, u.id, 'login', 'user', u.id, f'ip={client_ip(request)} code')
    s.commit()
    return {'access_token': token_for(u), 'token_type': 'bearer', 'role': u.role, 'username': u.username}


@app.get('/api/auth/options')
def auth_options():
    """What the sign-in page may offer."""
    return {'self_registration': SELF_REGISTRATION, 'email': MSG.email_channel() != 'log', 'sms': MSG.sms_channel() != 'log',
            'genders': list(GENDERS), 'districts': list(DISTRICTS)}


@app.get('/api/me')
def me(u: User = Depends(current_user), s: Session = Depends(db)):
    inst = s.get(Institution, u.institution_id)
    return {'id': u.id, 'username': u.username, 'role': u.role, 'full_name': u.full_name,
            'institution': {'id': inst.id, 'name': inst.name, 'code': inst.code, 'ai_enabled': inst.ai_enabled}}


def prefs_dict(u: User) -> dict:
    return {'full_name': u.full_name, 'email': u.email, 'phone': u.phone, 'email_verified': u.email_verified,
            'pending_email': u.pending_email, 'alert_email': u.alert_email, 'alert_sms': u.alert_sms,
            'otp_login': u.otp_login, 'gender': u.gender, 'birth_year': u.birth_year, 'district': u.district,
            'genders': list(GENDERS), 'districts': list(DISTRICTS), 'channels': {'email': MSG.email_channel() != 'log', 'sms': MSG.sms_channel() != 'log'}}


@app.get('/api/me/preferences')
def get_preferences(u: User = Depends(current_user)):
    return prefs_dict(u)


@app.put('/api/me/preferences')
def set_preferences(req: PreferencesUpdate, u: User = Depends(current_user), s: Session = Depends(db)):
    """Contact details and alerts. A new email address only takes over once its code is confirmed."""
    out = {}
    if req.full_name is not None:
        u.full_name = req.full_name.strip()
    if req.phone is not None:
        _, phone = clean_contact('', req.phone)
        u.phone = phone or None
    if req.email is not None:
        email, _ = clean_contact(req.email, '')
        if email and email != (u.email or ''):
            if email_taken(s, email, except_id=u.id):
                raise HTTPException(409, 'Another account already uses that email address.')
            if not rate_ok(f'emailchange:{u.id}', 3, 900):
                raise HTTPException(429, 'Too many changes. Try again later.')
            u.pending_email = email
            out = send_code(s, u, 'email', 'email', email)
    if req.alert_email is not None:
        u.alert_email = req.alert_email
    if req.alert_sms is not None:
        if req.alert_sms and not u.phone:
            raise HTTPException(422, 'Add a phone number first to get SMS alerts.')
        u.alert_sms = req.alert_sms
    if req.otp_login is not None:
        if req.otp_login and not (u.email and u.email_verified):
            raise HTTPException(422, 'Confirm an email address first: the sign-in code is sent there.')
        u.otp_login = req.otp_login
    fields = req.model_fields_set
    for k, v in clean_profile(req.gender, req.birth_year if 'birth_year' in fields else None, req.district).items():
        setattr(u, k, v)
    if 'birth_year' in fields and req.birth_year is None:
        u.birth_year = None
    audit(s, u.institution_id, u.id, 'update_preferences', 'user', u.id,
          json.dumps({k: v for k, v in req.model_dump().items() if v is not None and k not in ('email', 'phone', 'gender', 'birth_year', 'district')}))
    s.commit()
    return {**prefs_dict(u), **out}


@app.post('/api/me/email/verify')
def confirm_new_email(req: EmailCode, request: Request, u: User = Depends(current_user), s: Session = Depends(db)):
    if not rate_ok(f'code:{client_ip(request)}', 20, 900):
        raise HTTPException(429, 'Too many attempts. Try again later.')
    if not u.pending_email or not check_code(s, u, 'email', req.code):
        s.commit()
        raise HTTPException(400, 'That code is not right, or it has expired.')
    u.email, u.pending_email, u.email_verified = u.pending_email, None, True
    audit(s, u.institution_id, u.id, 'change_email', 'user', u.id)
    s.commit()
    return prefs_dict(u)


@app.get('/api/admin/messages')
def message_log(limit: int = Query(100, ge=1, le=500), u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    """Emails and SMS the portal sent or recorded. Codes are always masked."""
    rows = s.scalars(select(OutboundMessage).where(OutboundMessage.institution_id == u.institution_id)
                     .order_by(OutboundMessage.created_at.desc()).limit(limit)).all()
    counts = dict(s.execute(select(OutboundMessage.status, func.count()).where(OutboundMessage.institution_id == u.institution_id)
                            .group_by(OutboundMessage.status)).all())
    return {'counts': counts, 'channels': {'email': MSG.email_channel(), 'sms': MSG.sms_channel()},
            'items': [{'id': m.id, 'channel': m.channel, 'kind': m.kind, 'to': m.to_addr, 'subject': m.subject, 'body': m.body,
                       'status': m.status, 'detail': m.detail, 'created_at': iso(m.created_at), 'sent_at': iso(m.sent_at)}
                      for m in rows]}


@app.post('/api/me/password')
def change_password(req: PasswordChange, u: User = Depends(current_user), s: Session = Depends(db)):
    if not verify_password(req.current_password, u.password_hash):
        raise HTTPException(400, 'Current password is incorrect.')
    check_password_policy(req.new_password, u.username)
    u.password_hash = hash_password(req.new_password)
    u.token_version += 1  # signs out every other session
    audit(s, u.institution_id, u.id, 'change_password', 'user', u.id)
    s.commit()
    return {'status': 'changed', 'access_token': token_for(u)}


def user_dict(x: User) -> dict:
    return {'id': x.id, 'username': x.username, 'role': x.role, 'active': x.active, 'created_at': iso(x.created_at),
            'full_name': x.full_name, 'email': x.email, 'phone': x.phone, 'email_verified': x.email_verified}


@app.get('/api/users')
def list_users(u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    rows = s.scalars(select(User).where(User.institution_id == u.institution_id).order_by(User.username)).all()
    return [user_dict(x) for x in rows]


@app.post('/api/users')
def create_user(req: UserCreate, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    if req.role not in ROLES:
        raise HTTPException(422, f'role must be one of {list(ROLES)}')
    check_password_policy(req.password, req.username)
    if s.scalar(select(User).where(func.lower(User.username) == req.username.lower())):
        raise HTTPException(409, 'That username is already taken.')
    email, phone = clean_contact(req.email, req.phone)
    if email and email_taken(s, email):
        raise HTTPException(409, 'Another account already uses that email address.')
    x = User(username=req.username, password_hash=hash_password(req.password), role=req.role, institution_id=u.institution_id,
             full_name=req.full_name.strip(), email=email or None, phone=phone or None)
    s.add(x)
    s.flush()
    audit(s, u.institution_id, u.id, 'create', 'user', x.id, json.dumps({'username': x.username, 'role': x.role}))
    s.commit()
    return user_dict(x)


@app.patch('/api/users/{user_id}')
def update_user(user_id: int, req: UserUpdate, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    x = s.get(User, user_id)
    if not x or x.institution_id != u.institution_id:
        raise HTTPException(404, 'User not found')
    if x.id == u.id and (req.active is False or (req.role and req.role != 'admin')):
        raise HTTPException(400, 'You cannot deactivate or demote your own account.')
    changes = {}
    if req.role is not None:
        if req.role not in ROLES:
            raise HTTPException(422, f'role must be one of {list(ROLES)}')
        x.role, changes['role'] = req.role, req.role
    if req.active is not None:
        x.active, changes['active'] = req.active, req.active
    if req.password:
        check_password_policy(req.password, x.username)
        x.password_hash, changes['password'] = hash_password(req.password), 'reset'
    x.token_version += 1
    audit(s, u.institution_id, u.id, 'update', 'user', x.id, json.dumps(changes))
    s.commit()
    return user_dict(x)


@app.get('/api/institution')
def get_institution(u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    inst = s.get(Institution, u.institution_id)
    return {'id': inst.id, 'name': inst.name, 'code': inst.code, 'ai_enabled': inst.ai_enabled,
            'ai_configured': bool(os.getenv('OPENAI_API_KEY') and os.getenv('OPENAI_MODEL') and OpenAI),
            'ai_model': os.getenv('OPENAI_MODEL') or None}


@app.put('/api/institution')
def update_institution(req: InstitutionSettings, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    inst = s.get(Institution, u.institution_id)
    inst.ai_enabled = req.ai_enabled
    audit(s, u.institution_id, u.id, 'update', 'institution', inst.id, json.dumps({'ai_enabled': req.ai_enabled}))
    s.commit()
    return {'ai_enabled': inst.ai_enabled}


@app.get('/api/config')
def platform_config(u: User = Depends(current_user)):
    return {'version': VERSION, 'institution_id': u.institution_id, 'max_upload_bytes': MAX_UPLOAD_BYTES,
            'human_decision_required': True}


# ----------------------------------------------------------------------------
# Programmes and rubric (the institution's standards)
# ----------------------------------------------------------------------------
def programme_dict(p: FundingProgram) -> dict:
    return {'id': p.id, 'name': p.name, 'code': p.code, 'description': p.description, 'min_request': p.min_request,
            'max_request': p.max_request, 'min_owner_contribution_pct': p.min_owner_contribution_pct, 'enabled': p.enabled}


@app.get('/api/programmes')
def list_programmes(u: User = Depends(current_user), s: Session = Depends(db)):
    q = select(FundingProgram).where(FundingProgram.institution_id == u.institution_id).order_by(FundingProgram.name)
    if u.role == 'applicant':
        q = q.where(FundingProgram.enabled == True)  # noqa: E712
    return [programme_dict(p) for p in s.scalars(q).all()]


@app.post('/api/programmes')
def create_programme(req: ProgramRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    p = FundingProgram(id=new_id(), institution_id=u.institution_id, **req.model_dump())
    s.add(p)
    audit(s, u.institution_id, u.id, 'create', 'funding_program', p.id, p.code)
    s.commit()
    return programme_dict(p)


@app.put('/api/programmes/{program_id}')
def update_programme(program_id: str, req: ProgramRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    p = s.get(FundingProgram, program_id)
    if not p or p.institution_id != u.institution_id:
        raise HTTPException(404, 'Funding programme not found')
    for k, v in req.model_dump().items():
        setattr(p, k, v)
    audit(s, u.institution_id, u.id, 'update', 'funding_program', p.id, json.dumps(req.model_dump()))
    s.commit()
    return programme_dict(p)


@app.get('/api/rubric')
def list_rubric(programme_id: str | None = None, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    q = select(RubricCriterion).where(RubricCriterion.institution_id == u.institution_id).order_by(RubricCriterion.created_at)
    if programme_id:
        q = q.where(or_(RubricCriterion.programme_id.is_(None), RubricCriterion.programme_id == programme_id))
    return [criterion_dict(c) for c in s.scalars(q).all()]


def _check_programme(s: Session, u: User, programme_id):
    if programme_id:
        p = s.get(FundingProgram, programme_id)
        if not p or p.institution_id != u.institution_id:
            raise HTTPException(404, 'Funding programme not found')


@app.post('/api/rubric')
def create_criterion(req: CriterionRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    validate_criterion(req)
    _check_programme(s, u, req.programme_id)
    dup = s.scalar(select(RubricCriterion).where(RubricCriterion.institution_id == u.institution_id,
                                                 RubricCriterion.key == req.key,
                                                 RubricCriterion.programme_id == req.programme_id if req.programme_id
                                                 else RubricCriterion.programme_id.is_(None)))
    if dup:
        raise HTTPException(409, 'A criterion with this key already exists for this scope.')
    c = RubricCriterion(id=new_id(), institution_id=u.institution_id, programme_id=req.programme_id, key=req.key,
                        label=req.label, description=req.description, kind=req.kind, metric=req.metric,
                        comparator=req.comparator, threshold=req.threshold,
                        keywords_json=json.dumps([k.strip() for k in req.keywords if k.strip()]),
                        mandatory=req.mandatory, weight=req.weight, enabled=req.enabled)
    s.add(c)
    audit(s, u.institution_id, u.id, 'create', 'rubric_criterion', c.id, req.key)
    s.commit()
    return criterion_dict(c)


@app.put('/api/rubric/{criterion_id}')
def update_criterion(criterion_id: str, req: CriterionRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    c = s.get(RubricCriterion, criterion_id)
    if not c or c.institution_id != u.institution_id:
        raise HTTPException(404, 'Criterion not found')
    validate_criterion(req)
    _check_programme(s, u, req.programme_id)
    c.programme_id, c.key, c.label, c.description, c.kind = req.programme_id, req.key, req.label, req.description, req.kind
    c.metric, c.comparator, c.threshold = req.metric, req.comparator, req.threshold
    c.keywords_json = json.dumps([k.strip() for k in req.keywords if k.strip()])
    c.mandatory, c.weight, c.enabled = req.mandatory, req.weight, req.enabled
    audit(s, u.institution_id, u.id, 'update', 'rubric_criterion', c.id, json.dumps(req.model_dump()))
    s.commit()
    return criterion_dict(c)


# ----------------------------------------------------------------------------
# Applications
# ----------------------------------------------------------------------------
def app_summary(a: Application) -> dict:
    return {'id': a.id, 'applicant_name': a.applicant_name, 'funding_request': a.funding_request,
            'owner_contribution': a.owner_contribution, 'status': a.status, 'programme_id': a.programme_id,
            'coverage_pct': a.coverage_pct, 'high_risks': a.high_risks, 'medium_risks': a.medium_risks,
            'idea_title': a.idea_title, 'sector': a.sector, 'stage': a.stage,
            'created_at': iso(a.created_at), 'updated_at': iso(a.updated_at)}


def idea_dict(a: Application) -> dict:
    return {'idea_title': a.idea_title, 'problem': a.problem, 'solution': a.solution, 'target_users': a.target_users,
            'sector': a.sector, 'stage': a.stage, 'answers': jl(a.extra_json, {})}


@app.post('/api/applications')
def create_application(req: ApplicationCreate, u: User = Depends(current_user), s: Session = Depends(db)):
    if req.programme_id:
        p = s.get(FundingProgram, req.programme_id)
        if not p or p.institution_id != u.institution_id or not p.enabled:
            raise HTTPException(404, 'Funding programme not found')
    if u.role == 'applicant':
        missing = missing_idea_fields(req)
        if missing:
            raise HTTPException(422, 'Please complete the required fields: ' + ', '.join(missing) + '.')
    answers = clean_answers(s, u.institution_id, req.answers, enforce_required=u.role == 'applicant')
    a = Application(extra_json=json.dumps(answers), id=new_id(), institution_id=u.institution_id, programme_id=req.programme_id,
                    applicant_name=req.applicant_name.strip(), funding_request=req.funding_request,
                    owner_contribution=req.owner_contribution, created_by=u.id,
                    idea_title=req.idea_title.strip(), problem=req.problem.strip(), solution=req.solution.strip(),
                    target_users=req.target_users.strip(), sector=req.sector.strip(), stage=req.stage.strip())
    s.add(a)
    audit(s, u.institution_id, u.id, 'create', 'application', a.id, 'Application created')
    s.commit()
    return {'id': a.id, 'status': a.status}


@app.get('/api/applications')
def list_applications(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), status: str | None = None,
                      u: User = Depends(current_user), s: Session = Depends(db)):
    cond = [Application.institution_id == u.institution_id]
    if u.role == 'applicant':
        cond.append(Application.created_by == u.id)
    elif u.role == 'officer':
        assigned = select(ReviewAssignment.application_id).where(ReviewAssignment.reviewer_id == u.id)
        cond.append(or_(Application.created_by == u.id, Application.id.in_(assigned)))
    if status:
        cond.append(Application.status == status)
    total = s.scalar(select(func.count()).select_from(Application).where(*cond))
    rows = s.scalars(select(Application).where(*cond).order_by(Application.created_at.desc()).limit(limit).offset(offset)).all()
    return {'items': [app_summary(a) for a in rows], 'total': total, 'limit': limit, 'offset': offset}


def latest_result(s: Session, a: Application) -> dict | None:
    if not a.latest_version:
        return None
    v = s.scalar(select(AssessmentVersion).where(AssessmentVersion.application_id == a.id,
                                                 AssessmentVersion.version == a.latest_version))
    r = jl(v.result_json) if v else None
    if r:
        r['assessment_version'] = a.latest_version
    return r


@app.get('/api/applications/{app_id}')
def get_application(app_id: str, u: User = Depends(current_user), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    docs = s.scalars(select(Document).where(Document.application_id == a.id).order_by(Document.version.desc())).all()
    out = {**app_summary(a), **idea_dict(a), 'documents': [{'id': d.id, 'name': d.original_name, 'version': d.version, 'size_bytes': d.size_bytes,
                                           'sha256': d.sha256, 'created_at': iso(d.created_at)} for d in docs]}
    if u.role == 'applicant':
        return {**out, 'assessment': None, 'allowed_actions': [],
                'can_edit': a.status in ('draft', 'request_information')}
    prog = s.get(FundingProgram, a.programme_id) if a.programme_id else None
    out.update({
        'assessment': latest_result(s, a), 'assessment_version': a.latest_version, 'referred_by': a.referred_by,
        'overrides': current_overrides(a), 'allowed_actions': allowed_decisions(a, u),
        'programme': programme_dict(prog) if prog else None,
        'eligibility': A.programme_eligibility(a.funding_request, a.owner_contribution, programme_dict(prog)) if prog else None,
        'can_score': can_score(s, a, u), 'is_assigned': is_assigned(s, a.id, u.id),
    })
    return out


@app.post('/api/applications/{app_id}/analyse-file')
def analyse_application_file(app_id: str, file: UploadFile = File(...), u: User = Depends(current_user), s: Session = Depends(db)):
    # Plain `def`: FastAPI runs it in a worker thread, so slow parsing / AI calls never block other users.
    if not rate_ok(f'analyse:{u.id}', 20, 600):
        raise HTTPException(429, 'Too many uploads; please wait a few minutes.')
    a = get_app_or_404(s, app_id, u)
    if a.status not in EDITABLE_STATUSES:
        raise HTTPException(409, f'Documents cannot be added while the application is "{a.status}".')
    if u.role == 'applicant' and a.status == 'under_review':
        raise HTTPException(409, 'Your idea is with the evaluation panel. You can upload again if it is sent back for revision.')
    data = file.file.read(MAX_UPLOAD_BYTES + 1)  # bounded read
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f'File exceeds the maximum upload size of {MAX_UPLOAD_BYTES} bytes.')
    inst = s.get(Institution, u.institution_id)
    result = process_upload(s, inst, a, u, data, file.filename or '', file.content_type or '')
    if u.role == 'applicant':
        return {'status': 'received', 'document_version': result['document_version'],
                'message': 'Your document was received and will be reviewed by an officer.'}
    return result


@app.post('/api/applications/{app_id}/financials')
def override_financials(app_id: str, req: FinancialsOverride, u: User = Depends(require_roles('officer', 'admin')),
                        s: Session = Depends(db)):
    """Officer confirms or corrects the revenue/expense figures. Creates a new assessment version."""
    a = get_app_or_404(s, app_id, u)
    if a.status not in EDITABLE_STATUSES:
        raise HTTPException(409, f'Figures cannot be changed while the application is "{a.status}".')
    prev = s.scalar(select(AssessmentVersion).where(AssessmentVersion.application_id == a.id,
                                                    AssessmentVersion.version == a.latest_version))
    doc = s.scalar(select(Document).where(Document.application_id == a.id).order_by(Document.version.desc()).limit(1))
    if not prev or not doc:
        raise HTTPException(400, 'Analyse a document first.')
    try:
        raw = (DOCS / doc.storage_name).read_bytes()
    except FileNotFoundError:
        raise HTTPException(410, 'The stored document is no longer available; upload it again.')
    text = extract_doc(raw, Path(doc.storage_name).suffix.lower())
    a.revenue_override, a.expenses_override = req.revenue, req.expenses
    criteria = load_criteria(s, a.institution_id, a.programme_id)
    det = A.analyse_document(text, a.funding_request, a.owner_contribution, criteria, current_overrides(a))
    ai_out = jl(prev.ai_output_json)
    ai_meta = (jl(prev.result_json, {}) or {}).get('ai', {'mode': 'rules_only'})
    result = store_assessment(s, a, u, det, ai_out, ai_meta, prev.ai_output_json, doc.id, 'financials_override')
    audit(s, a.institution_id, u.id, 'override_financials', 'application', a.id,
          json.dumps({'revenue': req.revenue, 'expenses': req.expenses, 'assessment_version': result['assessment_version']}))
    s.commit()
    return result


@app.get('/api/applications/{app_id}/versions')
def list_versions(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    rows = s.scalars(select(AssessmentVersion).where(AssessmentVersion.application_id == a.id)
                     .order_by(AssessmentVersion.version.desc())).all()
    out = []
    for v in rows:
        r = jl(v.result_json, {})
        out.append({'version': v.version, 'trigger': v.trigger, 'ai_mode': v.ai_mode, 'ai_model': v.ai_model,
                    'prompt_version': v.prompt_version, 'input_sha256': v.input_sha256, 'created_at': iso(v.created_at),
                    'coverage_pct': (r.get('rubric') or {}).get('coverage_pct'), 'risks': len(r.get('risks', []))})
    return out


@app.get('/api/applications/{app_id}/versions/{version}')
def get_version(app_id: str, version: int, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    v = s.scalar(select(AssessmentVersion).where(AssessmentVersion.application_id == a.id, AssessmentVersion.version == version))
    if not v:
        raise HTTPException(404, 'Assessment version not found')
    r = jl(v.result_json, {})
    r['assessment_version'] = v.version
    return r


@app.get('/api/applications/{app_id}/applicant-check')
def applicant_check(app_id: str, u: User = Depends(current_user), s: Session = Depends(db)):
    """Read-only readiness check (no writes on GET)."""
    a = get_app_or_404(s, app_id, u)
    issues = []
    if len(a.applicant_name.strip()) < 2:
        issues.append({'key': 'applicant_name', 'severity': 'high', 'message': 'Applicant name is missing.'})
    if a.funding_request <= 0:
        issues.append({'key': 'funding_request', 'severity': 'low', 'message': 'No funding amount has been stated (optional).'})
    if a.owner_contribution <= 0:
        issues.append({'key': 'owner_contribution', 'severity': 'low', 'message': 'No own contribution has been stated (optional).'})
    if not a.programme_id:
        issues.append({'key': 'programme', 'severity': 'medium', 'message': 'No funding programme has been selected.'})
    for label in missing_idea_fields(a):
        issues.append({'key': 'idea', 'severity': 'high', 'message': f'{label} is missing.'})
    n_docs = s.scalar(select(func.count()).select_from(Document).where(Document.application_id == a.id))
    if not n_docs:
        issues.append({'key': 'documents', 'severity': 'high', 'message': 'Upload your idea document before submitting.'})
    out = {'application_id': a.id, 'ready_for_officer_review': not any(x['severity'] == 'high' for x in issues),
           'issues': issues, 'document_count': n_docs}
    if u.role != 'applicant':
        out['due_diligence_open'] = s.scalar(select(func.count()).select_from(DueDiligenceQuestion).where(
            DueDiligenceQuestion.application_id == a.id, DueDiligenceQuestion.status == 'open'))
    return out


# ----------------------------------------------------------------------------
# Due diligence, claim evidence, decisions
# ----------------------------------------------------------------------------
@app.get('/api/applications/{app_id}/due-diligence')
def list_dd(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    rows = s.scalars(select(DueDiligenceQuestion).where(DueDiligenceQuestion.application_id == a.id)
                     .order_by(DueDiligenceQuestion.created_at)).all()
    return [{'id': x.id, 'question': x.question, 'category': x.category, 'rationale': x.rationale, 'priority': x.priority,
             'status': x.status, 'answer': x.answer, 'evidence_note': x.evidence_note, 'answered_at': iso(x.answered_at)}
            for x in rows]


@app.post('/api/applications/{app_id}/due-diligence/{question_id}/answer')
def answer_dd(app_id: str, question_id: str, req: DDAnswer, u: User = Depends(require_roles('officer', 'admin')),
              s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    q = s.get(DueDiligenceQuestion, question_id)
    if not q or q.application_id != a.id or q.institution_id != u.institution_id:
        raise HTTPException(404, 'Due diligence question not found')
    if q.status == 'superseded':
        raise HTTPException(409, 'This question no longer applies to the latest assessment.')
    has = bool(req.answer.strip())
    q.answer, q.evidence_note, q.status = req.answer, req.evidence_note, 'answered' if has else 'open'
    q.answered_by, q.answered_at = u.id, utcnow() if has else None
    audit(s, u.institution_id, u.id, 'answer', 'due_diligence_question', q.id, json.dumps({'status': q.status}))
    s.commit()
    return {'id': q.id, 'status': q.status}


@app.get('/api/applications/{app_id}/claim-evidence')
def list_claim_evidence(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    rows = s.scalars(select(ClaimEvidence).where(ClaimEvidence.application_id == a.id).order_by(ClaimEvidence.created_at.desc())).all()
    return [{'id': x.id, 'claim': x.claim, 'evidence_status': x.evidence_status, 'source': x.source, 'excerpt': x.excerpt,
             'note': x.note, 'verified_at': iso(x.verified_at)} for x in rows]


@app.post('/api/applications/{app_id}/claim-evidence')
def add_claim_evidence(app_id: str, req: ClaimEvidenceRequest, u: User = Depends(require_roles('officer', 'admin')),
                       s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    if req.evidence_status not in {'verified', 'partially_verified', 'unsupported', 'contradictory', 'unverified'}:
        raise HTTPException(400, 'Invalid evidence status')
    checked = req.evidence_status != 'unverified'
    x = ClaimEvidence(id=new_id(), institution_id=u.institution_id, application_id=a.id, claim=req.claim,
                      evidence_status=req.evidence_status, source=req.source, excerpt=req.excerpt, note=req.note,
                      verified_by=u.id if checked else None, verified_at=utcnow() if checked else None)
    s.add(x)
    audit(s, u.institution_id, u.id, 'create', 'claim_evidence', x.id, req.claim[:200])
    s.commit()
    return {'id': x.id}


@app.post('/api/applications/{app_id}/decision')
def decision(app_id: str, req: DecisionRequest, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    """The single decision endpoint: validates the transition, writes the DecisionRecord, updates the
    status, notifies people and audits - all in one transaction."""
    a = get_app_or_404(s, app_id, u)
    if req.decision not in DECISION_RULES:
        raise HTTPException(400, f'decision must be one of {list(DECISION_RULES)}')
    roles, statuses = DECISION_RULES[req.decision]
    if not a.latest_version:
        raise HTTPException(409, 'The application has no assessment yet.')
    if u.role not in roles:
        raise HTTPException(403, f'Your role cannot record "{req.decision}".')
    if a.status not in statuses:
        raise HTTPException(409, f'"{req.decision}" is not allowed while the application is "{a.status}".')
    if req.decision in ('approve', 'decline') and a.referred_by == u.id:
        raise HTTPException(403, 'The person who referred this application cannot also decide it (separation of duties).')
    s.add(DecisionRecord(id=new_id(), institution_id=u.institution_id, application_id=a.id, assessment_version=a.latest_version,
                         decision=req.decision, rationale=req.rationale.strip(), conditions=req.conditions, decided_by=u.id))
    a.status = req.decision
    if req.decision == 'refer_committee':
        a.referred_by = u.id
        notify(s, u.institution_id, staff_ids(s, u.institution_id, ('committee', 'admin'), u.id), a, 'workflow',
               f'{a.applicant_name} was referred to committee.')
    else:
        notify(s, u.institution_id, staff_ids(s, u.institution_id, ('officer', 'admin'), u.id), a, 'workflow',
               f'{a.applicant_name}: {req.decision.replace("_", " ")} recorded.')
    if req.decision in ('request_information', 'approve', 'decline'):
        notify(s, u.institution_id, [a.created_by], a, 'applicant', f'Update on your application: {req.decision.replace("_", " ")}.')
    audit(s, u.institution_id, u.id, 'human_decision', 'application', a.id,
          json.dumps({'decision': req.decision, 'assessment_version': a.latest_version}))
    s.commit()
    return {'status': a.status, 'recorded_by': u.username, 'assessment_version': a.latest_version}


@app.get('/api/applications/{app_id}/decisions')
def decision_records(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    names = {x.id: x.username for x in s.scalars(select(User).where(User.institution_id == u.institution_id)).all()}
    rows = s.scalars(select(DecisionRecord).where(DecisionRecord.application_id == a.id).order_by(DecisionRecord.created_at.desc())).all()
    return [{'id': x.id, 'decision': x.decision, 'rationale': x.rationale, 'conditions': x.conditions,
             'decided_by': names.get(x.decided_by, str(x.decided_by)), 'assessment_version': x.assessment_version,
             'created_at': iso(x.created_at)} for x in rows]


# ----------------------------------------------------------------------------
# Innovation portal: idea editing, timeline, reviewer assignment, human scoring
# ----------------------------------------------------------------------------
SCORE_STATUSES = {'officer': {'under_review'}, 'committee': {'refer_committee'}}
DISAGREEMENT_POINTS = 20.0  # flag when reviewers' weighted totals differ by this many points (of 100)


def can_score(s: Session, a: Application, u: User) -> bool:
    if a.status not in SCORE_STATUSES.get(u.role, set()):
        return False
    return u.role == 'committee' or is_assigned(s, a.id, u.id)


def score_criteria_rows(s: Session, inst_id: str, enabled_only: bool = True) -> list[ScoreCriterion]:
    q = select(ScoreCriterion).where(ScoreCriterion.institution_id == inst_id)
    if enabled_only:
        q = q.where(ScoreCriterion.enabled == True)  # noqa: E712
    return list(s.scalars(q.order_by(ScoreCriterion.weight.desc(), ScoreCriterion.label)).all())  # deterministic on every database


def score_criterion_dict(c: ScoreCriterion) -> dict:
    return {'id': c.id, 'key': c.key, 'label': c.label, 'description': c.description, 'weight': c.weight,
            'enabled': c.enabled}


def weighted_total(scores: dict[str, int], criteria: list[ScoreCriterion]) -> float | None:
    """Weighted score out of 100 over the criteria this reviewer has scored (1 = 20, 5 = 100)."""
    used = [c for c in criteria if c.id in scores]
    wsum = sum(c.weight for c in used)
    if not used or wsum <= 0:
        return None
    return round(sum(scores[c.id] / 5 * 100 * c.weight for c in used) / wsum, 1)


@app.put('/api/applications/{app_id}/idea')
def update_idea(app_id: str, req: IdeaUpdate, u: User = Depends(current_user), s: Session = Depends(db)):
    """The submitter edits the idea while it is a draft or has been sent back for revision."""
    a = get_app_or_404(s, app_id, u)
    if a.created_by != u.id and u.role != 'admin':
        raise HTTPException(403, 'Only the submitter can edit this idea.')
    if a.status not in ('draft', 'request_information'):
        raise HTTPException(409, f'The idea cannot be edited while it is "{a.status}".')
    for k in IDEA_TEXT_FIELDS:
        setattr(a, k, getattr(req, k).strip())
    if req.answers is not None:
        a.extra_json = json.dumps(clean_answers(s, a.institution_id, req.answers, enforce_required=True))
    audit(s, a.institution_id, u.id, 'edit_idea', 'application', a.id, 'Idea details updated')
    s.commit()
    return {**app_summary(a), **idea_dict(a)}


@app.get('/api/applications/{app_id}/timeline')
def timeline(app_id: str, u: User = Depends(current_user), s: Session = Depends(db)):
    """Status history. Submitters see feedback written when an idea is sent back, awarded or declined."""
    a = get_app_or_404(s, app_id, u)
    events = [{'at': iso(a.created_at), 'kind': 'created', 'status': 'draft', 'message': 'Idea created'}]
    for d in s.scalars(select(Document).where(Document.application_id == a.id)).all():
        events.append({'at': iso(d.created_at), 'kind': 'submitted', 'status': 'under_review',
                       'message': f'Document submitted (version {d.version}): {d.original_name}'})
    for r in s.scalars(select(DecisionRecord).where(DecisionRecord.application_id == a.id)).all():
        shareable = r.decision in ('request_information', 'approve', 'decline')
        events.append({'at': iso(r.created_at), 'kind': 'decision', 'status': r.decision,
                       'message': r.decision.replace('_', ' '),
                       'feedback': r.rationale if (shareable or u.role != 'applicant') else None})
    events.sort(key=lambda e: e['at'] or '')
    return {'status': a.status, 'events': events}


@app.get('/api/score-criteria')
def list_score_criteria(u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    return [score_criterion_dict(c) for c in score_criteria_rows(s, u.institution_id, enabled_only=False)]


@app.post('/api/score-criteria')
def create_score_criterion(req: ScoreCriterionRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    if s.scalar(select(ScoreCriterion).where(ScoreCriterion.institution_id == u.institution_id, ScoreCriterion.key == req.key)):
        raise HTTPException(409, 'A scoring criterion with this key already exists.')
    c = ScoreCriterion(id=new_id(), institution_id=u.institution_id, key=req.key, label=req.label.strip(),
                       description=req.description.strip(), weight=req.weight, enabled=req.enabled)
    s.add(c)
    audit(s, u.institution_id, u.id, 'create_score_criterion', 'score_criterion', c.id, req.key)
    s.commit()
    return score_criterion_dict(c)


@app.put('/api/score-criteria/{criterion_id}')
def update_score_criterion(criterion_id: str, req: ScoreCriterionRequest, u: User = Depends(require_roles('admin')),
                           s: Session = Depends(db)):
    c = s.get(ScoreCriterion, criterion_id)
    if not c or c.institution_id != u.institution_id:
        raise HTTPException(404, 'Scoring criterion not found')
    if req.key != c.key and s.scalar(select(ScoreCriterion).where(
            ScoreCriterion.institution_id == u.institution_id, ScoreCriterion.key == req.key)):
        raise HTTPException(409, 'A scoring criterion with this key already exists.')
    c.key, c.label, c.description, c.weight, c.enabled = req.key, req.label.strip(), req.description.strip(), req.weight, req.enabled
    audit(s, u.institution_id, u.id, 'update_score_criterion', 'score_criterion', c.id, req.key)
    s.commit()
    return score_criterion_dict(c)


def assignment_list(s: Session, a: Application) -> list[dict]:
    rows = s.execute(select(ReviewAssignment, User).join(User, User.id == ReviewAssignment.reviewer_id)
                     .where(ReviewAssignment.application_id == a.id).order_by(User.username)).all()
    scored = set(s.scalars(select(ReviewScore.reviewer_id).where(ReviewScore.application_id == a.id)).all())
    return [{'reviewer_id': usr.id, 'username': usr.username, 'assigned_at': iso(ra.created_at), 'has_scored': usr.id in scored}
            for ra, usr in rows]


@app.get('/api/applications/{app_id}/assignments')
def get_assignments(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    return assignment_list(s, get_app_or_404(s, app_id, u))


@app.put('/api/applications/{app_id}/assignments')
def set_assignments(app_id: str, req: AssignmentRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    """Admin sets the evaluators for an idea. Evaluators only see ideas assigned to them."""
    a = get_app_or_404(s, app_id, u)
    wanted = set(req.reviewer_ids)
    if wanted:
        ok = set(s.scalars(select(User.id).where(User.id.in_(wanted), User.institution_id == u.institution_id,
                                                 User.role == 'officer', User.active == True)).all())  # noqa: E712
        if ok != wanted:
            raise HTTPException(422, 'Reviewers must be active evaluators in your institution.')
    current = {r.reviewer_id: r for r in s.scalars(select(ReviewAssignment).where(ReviewAssignment.application_id == a.id)).all()}
    for rid, row in current.items():
        if rid not in wanted:
            s.delete(row)
    added = [rid for rid in wanted if rid not in current]
    for rid in added:
        s.add(ReviewAssignment(id=new_id(), institution_id=a.institution_id, application_id=a.id, reviewer_id=rid,
                               assigned_by=u.id))
    if added:
        notify(s, u.institution_id, added, a, 'assignment', f'You have been assigned to review "{a.idea_title or a.applicant_name}".')
    audit(s, u.institution_id, u.id, 'assign_reviewers', 'application', a.id, json.dumps({'reviewer_ids': sorted(wanted)}))
    s.commit()
    return assignment_list(s, a)


@app.put('/api/applications/{app_id}/scores')
def submit_scores(app_id: str, req: ScoreRequest, u: User = Depends(require_roles('officer', 'committee')),
                  s: Session = Depends(db)):
    """An evaluator (assigned officer) or adjudicator records 1-5 scores against the institution's criteria."""
    a = get_app_or_404(s, app_id, u)
    if u.role == 'officer' and not is_assigned(s, a.id, u.id):
        raise HTTPException(403, 'Only evaluators assigned to this idea can score it.')
    if not can_score(s, a, u):
        raise HTTPException(409, f'Scores cannot be recorded while the idea is "{a.status}".')
    criteria = {c.id: c for c in score_criteria_rows(s, u.institution_id)}
    ids = [x.criterion_id for x in req.scores]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, 'Each criterion may be scored once.')
    if any(i not in criteria for i in ids):
        raise HTTPException(422, 'Unknown or disabled scoring criterion.')
    existing = {r.criterion_id: r for r in s.scalars(select(ReviewScore).where(
        ReviewScore.application_id == a.id, ReviewScore.reviewer_id == u.id)).all()}
    for x in req.scores:
        row = existing.get(x.criterion_id)
        if row:
            row.score, row.comment = x.score, x.comment.strip()
        else:
            s.add(ReviewScore(id=new_id(), institution_id=a.institution_id, application_id=a.id, reviewer_id=u.id,
                              criterion_id=x.criterion_id, score=x.score, comment=x.comment.strip()))
    audit(s, u.institution_id, u.id, 'score', 'application', a.id,
          json.dumps({x.criterion_id: x.score for x in req.scores}, sort_keys=True))
    s.commit()
    return get_scores(app_id, u, s)


@app.get('/api/applications/{app_id}/scores')
def get_scores(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    """Evaluators see only their own scores (independent scoring). Adjudicators and admins see the panel."""
    a = get_app_or_404(s, app_id, u)
    criteria = score_criteria_rows(s, u.institution_id)
    rows = s.scalars(select(ReviewScore).where(ReviewScore.application_id == a.id)).all()
    names = {x.id: x.username for x in s.scalars(select(User).where(User.institution_id == u.institution_id)).all()}
    by_reviewer: dict[int, dict[str, ReviewScore]] = {}
    for r in rows:
        by_reviewer.setdefault(r.reviewer_id, {})[r.criterion_id] = r
    mine = by_reviewer.get(u.id, {})
    out = {'criteria': [score_criterion_dict(c) for c in criteria],
           'mine': {cid: {'score': r.score, 'comment': r.comment} for cid, r in mine.items()},
           'my_total': weighted_total({cid: r.score for cid, r in mine.items()}, criteria),
           'can_score': can_score(s, a, u), 'panel': None}
    if u.role in ('committee', 'admin'):
        reviewers = []
        for rid, sc in sorted(by_reviewer.items(), key=lambda kv: names.get(kv[0], '')):
            reviewers.append({'reviewer': names.get(rid, str(rid)),
                              'total': weighted_total({cid: r.score for cid, r in sc.items()}, criteria),
                              'complete': all(c.id in sc for c in criteria),
                              'scores': {cid: {'score': r.score, 'comment': r.comment} for cid, r in sc.items()}})
        per_criterion = {}
        for c in criteria:
            vals = [sc[c.id].score for sc in by_reviewer.values() if c.id in sc]
            per_criterion[c.id] = round(sum(vals) / len(vals), 2) if vals else None
        totals = [r['total'] for r in reviewers if r['total'] is not None]
        spread = round(max(totals) - min(totals), 1) if len(totals) > 1 else 0.0
        out['panel'] = {'reviewers': reviewers, 'criterion_averages': per_criterion,
                        'average_total': round(sum(totals) / len(totals), 1) if totals else None,
                        'spread': spread, 'disagreement': spread >= DISAGREEMENT_POINTS}
    return out


@app.post('/api/applications/{app_id}/knowledge-context')
def application_knowledge_context(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    r = latest_result(s, a) or {}
    query = ' '.join([a.applicant_name, r.get('business_model', ''), r.get('executive_summary', '')])
    items = retrieve_knowledge(s, u.institution_id, query)
    return {'application_id': a.id, 'items': items, 'method': 'institution-scoped keyword retrieval',
            'human_verification_required': True}


# ----------------------------------------------------------------------------
# Reports (no files written to disk; fetched with the Authorization header by the UI)
# ----------------------------------------------------------------------------
def report_context(s: Session, a: Application, u: User) -> dict:
    result = latest_result(s, a)
    if not result:
        raise HTTPException(400, 'Analyse the application first')
    names = {x.id: x.username for x in s.scalars(select(User).where(User.institution_id == u.institution_id)).all()}
    inst = s.get(Institution, u.institution_id)
    dd = s.scalars(select(DueDiligenceQuestion).where(DueDiligenceQuestion.application_id == a.id,
                                                      DueDiligenceQuestion.status != 'superseded')
                   .order_by(DueDiligenceQuestion.created_at)).all()
    claims = s.scalars(select(ClaimEvidence).where(ClaimEvidence.application_id == a.id)).all()
    decisions = s.scalars(select(DecisionRecord).where(DecisionRecord.application_id == a.id)
                          .order_by(DecisionRecord.created_at.desc())).all()
    return {'a': a, 'result': result, 'dd': dd, 'claims': claims, 'decisions': decisions, 'names': names,
            'institution': inst.name, 'generated': utcnow().strftime('%Y-%m-%d %H:%M UTC')}


def _fmt_metric(k: str, v) -> str:
    if v is None:
        return 'not available'
    if k in ('margin', 'owner_contribution_pct'):
        return f'{v:.1f}%'
    if k in ('funding_to_contribution', 'expense_to_revenue_ratio'):
        return f'{v:.2f}x'
    return f'{v:,.0f}'


def report_html(c: dict) -> str:
    e = lambda x: htmllib.escape(str(x if x is not None else ''))  # noqa: E731
    a, r = c['a'], c['result']
    src = r.get('sources', {})
    metrics = ''.join(f"<tr><th>{e(k.replace('_', ' ').title())}</th><td>{e(_fmt_metric(k, v))}</td>"
                      f"<td>{e((src.get(k) or {}).get('origin', '').replace('_', ' '))}"
                      f"{' - <i>' + e(src[k]['line']) + '</i>' if (src.get(k) or {}).get('line') else ''}</td></tr>"
                      for k, v in r['metrics'].items())
    warn = ''
    if r.get('needs_confirmation'):
        warn = (f"<p class='warn'>Figures read from the document and not yet confirmed by an officer: "
                f"{e(', '.join(r['needs_confirmation']))}.</p>")
    crit = ''.join(f"<tr><td>{e(x['label'])}{' *' if x['mandatory'] else ''}</td><td>{e(x['status'].replace('_', ' '))}</td>"
                   f"<td>{e(x['detail'])}{(' <i>AI: ' + e(x['ai_note']) + '</i>') if x.get('ai_note') else ''}</td></tr>"
                   for x in r['rubric']['criteria'])
    risks = ''.join(f"<li><b>{e(x['severity'])}</b> {e(x['title'])}: {e(x.get('detail', ''))}"
                    f" <span class='muted'>[{e(x.get('source', ''))}]</span></li>" for x in r['risks']) or '<li>None recorded.</li>'
    strengths = ''.join(f'<li>{e(x)}</li>' for x in r['strengths']) or '<li>None recorded.</li>'
    dq = ''.join(f'<tr><td>{e(q.priority)}</td><td>{e(q.question)}</td><td>{e(q.status)}</td><td>{e(q.answer)}</td></tr>'
                 for q in c['dd']) or '<tr><td colspan="4">None.</td></tr>'
    ce = ''.join(f'<tr><td>{e(x.claim)}</td><td>{e(x.evidence_status)}</td><td>{e(x.source)}</td><td>{e(x.note)}</td></tr>'
                 for x in c['claims']) or '<tr><td colspan="4">No officer-verified evidence records yet.</td></tr>'
    dr = ''.join(f"<div class='box'><b>{e(x.decision.replace('_', ' '))}</b> by {e(c['names'].get(x.decided_by, x.decided_by))} "
                 f"on {e(x.created_at.strftime('%Y-%m-%d %H:%M'))} UTC (assessment v{e(x.assessment_version)})"
                 f"<p>{e(x.rationale)}</p><p><b>Conditions:</b> {e(x.conditions)}</p></div>" for x in c['decisions']) \
        or '<p>No decision has been recorded.</p>'
    ai = r.get('ai', {})
    cov = r['rubric']['coverage_pct']
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Committee Report - {e(a.applicant_name)}</title>
<style>body{{font-family:Arial,sans-serif;color:#1d2939;margin:40px;line-height:1.45}}h2{{border-bottom:1px solid #ddd;padding-bottom:6px;margin-top:28px}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border:1px solid #ddd;text-align:left;vertical-align:top}}.muted{{color:#667085}}
.box{{background:#f6f8fa;padding:14px;border-radius:8px;margin:8px 0}}.warn{{background:#fff2df;padding:10px;border-radius:8px}}</style></head><body>
<h1>FundingLens AI - Committee Assessment Report</h1><p class="muted">{e(c['institution'])} - generated {e(c['generated'])} - platform v{e(VERSION)}</p>
<h2>1. Application</h2><table><tr><th>Applicant</th><td>{e(a.applicant_name)}</td></tr><tr><th>Application ID</th><td>{e(a.id)}</td></tr>
<tr><th>Status</th><td>{e(a.status)}</td></tr><tr><th>Funding request</th><td>{a.funding_request:,.2f}</td></tr>
<tr><th>Owner contribution</th><td>{a.owner_contribution:,.2f}</td></tr><tr><th>Assessment version</th><td>{e(r.get('assessment_version'))}</td></tr></table>
<h2>2. Executive assessment</h2><div class="box">{e(r['executive_summary'])}</div><p><b>Business model:</b> {e(r['business_model'])}</p>
<p class="muted">Mode: {e(ai.get('mode'))} {e(ai.get('model') or '')} - prompt {e(ai.get('prompt_version') or 'n/a')}</p>
<h2>3. Financial analysis</h2>{warn}<table><tr><th>Metric</th><th>Value</th><th>Source</th></tr>{metrics}</table>
<h2>4. Standards (rubric) - criteria coverage {e(str(cov) + '%' if cov is not None else 'n/a')}</h2>
<p class="muted">* mandatory. "Mentioned" is a keyword match, not verified evidence. Coverage is an indicator, not a recommendation.</p>
<table><tr><th>Criterion</th><th>Result</th><th>Detail</th></tr>{crit}</table>
<h2>5. Strengths</h2><ul>{strengths}</ul><h2>6. Risks</h2><ul>{risks}</ul>
<h2>7. Due diligence</h2><table><tr><th>Priority</th><th>Question</th><th>Status</th><th>Answer</th></tr>{dq}</table>
<h2>8. Claim-to-evidence traceability</h2><table><tr><th>Claim</th><th>Status</th><th>Source</th><th>Note</th></tr>{ce}</table>
<h2>9. Decision record</h2>{dr}<h2>10. Governance</h2><p>{e(r['governance'])}</p></body></html>"""


@app.get('/api/applications/{app_id}/committee-report', response_class=HTMLResponse)
def committee_report(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    a = get_app_or_404(s, app_id, u)
    html = report_html(report_context(s, a, u))
    audit(s, u.institution_id, u.id, 'generate', 'committee_report', a.id, 'HTML')
    s.commit()
    return HTMLResponse(html)


@app.get('/api/applications/{app_id}/committee-report.docx')
def committee_report_docx(app_id: str, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    from docx import Document as DocxDocument
    a = get_app_or_404(s, app_id, u)
    c = report_context(s, a, u)
    r = c['result']
    d = DocxDocument()
    d.add_heading('FundingLens AI - Committee Assessment Report', 0)
    d.add_paragraph(f"{c['institution']} - generated {c['generated']} - platform v{VERSION}")
    d.add_heading('1. Application', 1)
    for k, v in [('Applicant', a.applicant_name), ('Application ID', a.id), ('Status', a.status),
                 ('Funding request', f'{a.funding_request:,.2f}'), ('Owner contribution', f'{a.owner_contribution:,.2f}'),
                 ('Assessment version', r.get('assessment_version'))]:
        d.add_paragraph(f'{k}: {v}')
    d.add_heading('2. Executive assessment', 1)
    d.add_paragraph(r['executive_summary'])
    d.add_paragraph('Business model: ' + r['business_model'])
    d.add_heading('3. Financial analysis', 1)
    if r.get('needs_confirmation'):
        d.add_paragraph('Figures read from the document and not yet confirmed by an officer: ' + ', '.join(r['needs_confirmation']))
    t = d.add_table(rows=1, cols=3)
    t.style = 'Table Grid'
    for i, h in enumerate(('Metric', 'Value', 'Source')):
        t.rows[0].cells[i].text = h
    for k, v in r['metrics'].items():
        cells = t.add_row().cells
        cells[0].text, cells[1].text = k.replace('_', ' ').title(), _fmt_metric(k, v)
        cells[2].text = (r.get('sources', {}).get(k) or {}).get('origin', '').replace('_', ' ')
    cov = r['rubric']['coverage_pct']
    d.add_heading(f"4. Standards (rubric) - coverage {str(cov) + '%' if cov is not None else 'n/a'}", 1)
    d.add_paragraph('* mandatory. "Mentioned" is a keyword match, not verified evidence. Coverage is an indicator, not a recommendation.')
    for x in r['rubric']['criteria']:
        d.add_paragraph(f"{x['label']}{' *' if x['mandatory'] else ''}: {x['status'].replace('_', ' ')} - {x['detail']}", style='List Bullet')
    d.add_heading('5. Strengths', 1)
    for x in r['strengths']:
        d.add_paragraph(str(x), style='List Bullet')
    d.add_heading('6. Risks', 1)
    for x in r['risks']:
        d.add_paragraph(f"[{x['severity']}] {x['title']}: {x.get('detail', '')}", style='List Bullet')
    d.add_heading('7. Due diligence', 1)
    for q in c['dd']:
        d.add_paragraph(f"[{q.priority.upper()}] {q.question} - {q.status}" + (f" - Answer: {q.answer}" if q.answer else ''))
    d.add_heading('8. Claim-to-evidence traceability', 1)
    for x in c['claims']:
        d.add_paragraph(f'{x.claim} | {x.evidence_status} | {x.source} | {x.note}', style='List Bullet')
    d.add_heading('9. Decision record', 1)
    for x in c['decisions']:
        d.add_paragraph(f"{x.decision.replace('_', ' ')} by {c['names'].get(x.decided_by, x.decided_by)} on "
                        f"{x.created_at.strftime('%Y-%m-%d %H:%M')} UTC (assessment v{x.assessment_version})\n"
                        f"Rationale: {x.rationale}\nConditions: {x.conditions}")
    d.add_heading('10. Governance', 1)
    d.add_paragraph(r['governance'])
    buf = io.BytesIO()
    d.save(buf)
    audit(s, u.institution_id, u.id, 'generate', 'committee_report', a.id, 'DOCX')
    s.commit()
    return Response(buf.getvalue(), media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    headers={'Content-Disposition': f'attachment; filename="FundingLens_Committee_Report_{a.id[:8]}.docx"'})


# ----------------------------------------------------------------------------
# Knowledge, notifications, audit, dashboards
# ----------------------------------------------------------------------------
@app.get('/api/knowledge')
def list_knowledge(u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    rows = s.scalars(select(KnowledgeItem).where(KnowledgeItem.institution_id == u.institution_id)
                     .order_by(KnowledgeItem.created_at.desc())).all()
    return [{'id': x.id, 'title': x.title, 'category': x.category, 'version': x.version, 'content': x.content,
             'active': x.active, 'created_at': iso(x.created_at)} for x in rows]


@app.post('/api/knowledge')
def add_knowledge(req: KnowledgeRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    x = KnowledgeItem(id=new_id(), institution_id=u.institution_id, created_by=u.id, **req.model_dump())
    s.add(x)
    audit(s, u.institution_id, u.id, 'create', 'knowledge_item', x.id, x.title)
    s.commit()
    return {'id': x.id}


@app.put('/api/knowledge/{item_id}')
def update_knowledge(item_id: str, req: KnowledgeRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    x = s.get(KnowledgeItem, item_id)
    if not x or x.institution_id != u.institution_id:
        raise HTTPException(404, 'Knowledge item not found')
    for k, v in req.model_dump().items():
        setattr(x, k, v)
    audit(s, u.institution_id, u.id, 'update', 'knowledge_item', x.id, x.title)
    s.commit()
    return {'id': x.id}


@app.get('/api/notifications')
def notifications(u: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(Notification).where(Notification.institution_id == u.institution_id, Notification.user_id == u.id)
                     .order_by(Notification.created_at.desc()).limit(50)).all()
    return [{'id': n.id, 'kind': n.kind, 'message': n.message, 'read': n.read, 'application_id': n.application_id,
             'created_at': iso(n.created_at)} for n in rows]


@app.post('/api/notifications/{nid}/read')
def notification_read(nid: int, u: User = Depends(current_user), s: Session = Depends(db)):
    n = s.get(Notification, nid)
    if not n or n.institution_id != u.institution_id or n.user_id != u.id:
        raise HTTPException(404, 'Notification not found')
    n.read = True
    s.commit()
    return {'ok': True}


# ----------------------------------------------------------------------------
# Help, FAQs, contacts, support requests and administrator-managed content (1.5)
# ----------------------------------------------------------------------------
DEFAULT_SUPPORT_CATEGORIES = [
    {'key': 'account', 'label': 'My account or signing in', 'days': 1},
    {'key': 'technical', 'label': 'Something on the portal is not working', 'days': 1},
    {'key': 'submission', 'label': 'Submitting or editing my idea', 'days': 2},
    {'key': 'review', 'label': 'Where my idea is in the review', 'days': 3},
    {'key': 'other', 'label': 'Anything else', 'days': 3},
]
CONTENT_DEFAULTS = {
    'announcement': '',
    'welcome': ('Welcome to the CEDA Innovation Management Portal. Tell us about your idea, upload any supporting '
                'document, and follow every step of the review here. You will get an alert each time something changes.'),
    'support_intro': ('Need help? Check the questions below first. If your question is not answered, send it to us from '
                      'the Support page and we will reply within the time shown for that kind of question.'),
    'support_hours': 'Monday to Friday, 08:00 to 17:00, except public holidays.',
    'support_email': '',
    'support_phone': '',
    'support_categories': json.dumps(DEFAULT_SUPPORT_CATEGORIES),
}
PUBLIC_CONTENT = ('announcement', 'welcome', 'support_intro', 'support_hours', 'support_email', 'support_phone')
FIELD_KINDS = ('text', 'textarea', 'number', 'select', 'yes_no', 'date', 'url')
SUPPORT_ROLES = ('admin', 'officer')

# Public contact details checked against the organisations' published notices on 13 November 2026.
# Administrators can edit or hide any of them in Site content.
DEFAULT_CONTACTS = [
    ('Citizen Entrepreneurial Development Agency (CEDA)', 'Funding, mentoring and the innovation programme itself.',
     '+267 317 0895', '', 'https://www.ceda.co.bw', 'Gaborone CBD'),
    ('Companies and Intellectual Property Authority (CIPA)',
     'Registering a company or business name, and protecting your idea with a patent, trade mark or industrial design.',
     '+267 367 3700', 'info@cipa.co.bw', 'https://www.cipa.co.bw', 'Plot 54358, Prime Plaza, CBD, Gaborone'),
    ('Botswana Bureau of Standards (BOBS)', 'National standards, product testing and certification marks.',
     '+267 390 3200 / toll-free 0800 600 900', 'info@hq.bobstandards.bw', 'https://www.bobstandards.bw',
     'Plot 55745, Block 8, Main Airport Road, Gaborone'),
    ('Botswana Communications Regulatory Authority (BOCRA)',
     'Telecommunications, internet and ICT services, radio, broadcasting, postal services, equipment type approval and .bw domains.',
     '+267 395 7755', '', 'https://www.bocra.org.bw', 'Plot 50671, Independence Avenue, Gaborone'),
    ('Competition and Consumer Authority (CCA)', 'Fair competition and consumer protection.',
     '+267 393 4278', 'info@cca.co.bw', 'https://www.cca.co.bw', 'Plot 28, Matsitama Road, Main Mall, Gaborone'),
    ('Botswana Unified Revenue Service (BURS)', 'Tax registration, VAT and tax clearance certificates.',
     '+267 363 8000', '', 'https://www.burs.org.bw', 'Plot 53976, Kudumatse Drive, Gaborone'),
    ('Non-Bank Financial Institutions Regulatory Authority (NBFIRA)',
     'Fintech ideas that touch insurance, pensions, micro-lending or investments.',
     '+267 310 2595', 'info@nbfira.org.bw', 'https://www.nbfira.org.bw',
     '3rd Floor, Exponential Building, Plot 54351, New CBD, Gaborone'),
]
DEFAULT_FAQS = [
    ('Getting started', 'Who can submit an idea?',
     'Anyone with an innovative idea can create an account and submit it. Each call (programme) may set its own rules, '
     'so read the call description before you start.'),
    ('Getting started', 'How do I create an account?',
     'Choose "Create an account" on the sign-in page, fill in your details and type in the 6-digit code we email you. '
     'Your account works as soon as the code is confirmed.'),
    ('Getting started', 'I forgot my password. What do I do?',
     'Choose "Forgot password?" on the sign-in page. We send a reset code to your email address (or to your phone if '
     'you have no email on the account). The code works once and expires after 30 minutes.'),
    ('My idea', 'What do I need to submit?',
     'The idea title, the problem it solves, your solution, who it is for, the sector and how far along it is. You can '
     'also upload a supporting document such as a concept note or business plan (PDF, Word or text).'),
    ('My idea', 'Can I change my idea after I submit it?',
     'Not while it is with the review panel. If the panel sends it back for more information, you can edit it and '
     'submit again. Every change is kept in the timeline.'),
    ('My idea', 'How will I know what is happening with my idea?',
     'Open the idea to see its timeline and any feedback. You also get an alert in the portal and, if you choose it in '
     'Settings, by email or SMS each time the status changes.'),
    ('My idea', 'How are ideas judged?',
     'Evaluators assigned to your idea score it against published criteria, which you can ask about through Support. '
     'Adjudicators make the final decision. The person who refers an idea cannot also approve it.'),
    ('Protecting your idea', 'How can I protect my idea?',
     'An idea on its own cannot be registered, but an invention can be patented and a brand or logo can be registered as a '
     'trade mark. Both are handled by CIPA (see contacts below). Talk to CIPA before you share technical details widely.'),
    ('Rules and regulators', 'Do I need a registered company?',
     'Not to submit an idea. If your idea is funded or you start trading, you will need to register a company or '
     'business name with CIPA and register for tax with BURS.'),
    ('Rules and regulators', 'My product is an app, a device or uses mobile networks. Who regulates that?',
     'BOCRA regulates telecommunications, internet and ICT services, and approves radio and telecoms equipment. If your '
     'product handles payments, insurance or lending, also check with NBFIRA or the Bank of Botswana.'),
    ('Rules and regulators', 'My product will be sold to the public. Are there standards it must meet?',
     'Some products must meet compulsory Botswana standards. BOBS can tell you which standards apply and how to get '
     'your product tested and certified.'),
    ('Rules and regulators', 'Who looks after personal data?',
     'The Data Protection Act, 2024 applies to anyone who collects personal data. The Information and Data Protection '
     'Commission supervises it. If your idea collects personal data, plan how you will protect it from the start.'),
    ('Support', 'How quickly will I get a reply?',
     'Each kind of question has its own reply time, shown on the Support page. Times are in working days, Monday to Friday.'),
]


def add_working_days(start: datetime, days: int) -> datetime:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def seed_content(s: Session, inst: Institution):
    have = set(s.scalars(select(SiteContent.key).where(SiteContent.institution_id == inst.id)).all())
    for k, v in CONTENT_DEFAULTS.items():
        if k not in have:
            s.add(SiteContent(institution_id=inst.id, key=k, value=v))
    if not s.scalar(select(FaqItem.id).where(FaqItem.institution_id == inst.id).limit(1)):
        for i, (cat, q, a) in enumerate(DEFAULT_FAQS):
            s.add(FaqItem(id=new_id(), institution_id=inst.id, category=cat, question=q, answer=a, sort=(i + 1) * 10))
    if not s.scalar(select(Contact.id).where(Contact.institution_id == inst.id).limit(1)):
        for i, (name, purpose, phone, email, web, addr) in enumerate(DEFAULT_CONTACTS):
            s.add(Contact(id=new_id(), institution_id=inst.id, name=name, purpose=purpose, phone=phone, email=email,
                          website=web, address=addr, sort=(i + 1) * 10))


def content_map(s: Session, inst_id: str) -> dict:
    rows = s.scalars(select(SiteContent).where(SiteContent.institution_id == inst_id)).all()
    return {**CONTENT_DEFAULTS, **{r.key: r.value for r in rows}}


def support_categories(s: Session, inst_id: str) -> list[dict]:
    return jl(content_map(s, inst_id).get('support_categories'), DEFAULT_SUPPORT_CATEGORIES) or DEFAULT_SUPPORT_CATEGORIES


def field_dict(f: FormField) -> dict:
    return {'id': f.id, 'key': f.key, 'label': f.label, 'help': f.help, 'kind': f.kind, 'options': jl(f.options_json, []),
            'required': f.required, 'sort': f.sort, 'enabled': f.enabled}


def clean_answers(s: Session, inst_id: str, answers: dict | None, enforce_required: bool) -> dict:
    """Keep only answers to enabled questions, check their type, and (for submitters) that required ones are filled."""
    answers = answers or {}
    fields = s.scalars(select(FormField).where(FormField.institution_id == inst_id, FormField.enabled == True)  # noqa: E712
                       .order_by(FormField.sort, FormField.label)).all()
    out, missing, bad = {}, [], []
    for f in fields:
        v = answers.get(f.key)
        if isinstance(v, str):
            v = v.strip()
        if v in (None, '', []):
            if f.required and enforce_required:
                missing.append(f.label)
            continue
        if f.kind == 'number':
            try:
                v = float(v)
                if v != v or abs(v) > 1e12:
                    raise ValueError
            except (TypeError, ValueError):
                bad.append(f'{f.label} must be a number')
                continue
        elif f.kind == 'yes_no':
            if isinstance(v, str):
                v = v.lower() in ('yes', 'true', '1')
            v = bool(v)
        elif f.kind == 'select':
            if str(v) not in jl(f.options_json, []):
                bad.append(f'{f.label}: choose one of the options')
                continue
            v = str(v)
        elif f.kind == 'date':
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(v)):
                bad.append(f'{f.label} must be a date')
                continue
        elif f.kind == 'url':
            if not re.match(r'^https?://[^\s]+$', str(v)):
                bad.append(f'{f.label} must be a web address starting with http:// or https://')
                continue
        else:
            v = str(v)[:5000 if f.kind == 'textarea' else 500]
        out[f.key] = v
    if missing:
        raise HTTPException(422, 'Please complete the required fields: ' + ', '.join(missing) + '.')
    if bad:
        raise HTTPException(422, '; '.join(bad) + '.')
    return out


def notify_user(s: Session, inst_id: str, user_id: int, kind: str, message: str, subject: str):
    s.add(Notification(institution_id=inst_id, user_id=user_id, kind=kind, message=message))
    target = s.get(User, user_id)
    if target and target.active:
        queue_alert(s, target, subject, message)


class FormFieldRequest(BaseModel):
    key: str = Field(min_length=2, max_length=60, pattern=r'^[a-z][a-z0-9_]*$')
    label: str = Field(min_length=2, max_length=200)
    help: str = Field(default='', max_length=1000)
    kind: str = 'text'
    options: list[str] = Field(default_factory=list, max_length=50)
    required: bool = False
    sort: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class FaqRequest(BaseModel):
    category: str = Field(default='General', min_length=1, max_length=80)
    question: str = Field(min_length=5, max_length=300)
    answer: str = Field(min_length=5, max_length=5000)
    sort: int = Field(default=100, ge=0, le=10000)
    published: bool = True


class ContactRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    purpose: str = Field(default='', max_length=1000)
    phone: str = Field(default='', max_length=120)
    email: str = Field(default='', max_length=200)
    website: str = Field(default='', max_length=200)
    address: str = Field(default='', max_length=300)
    sort: int = Field(default=100, ge=0, le=10000)
    published: bool = True


class TicketRequest(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=10, max_length=5000)
    application_id: str | None = None


class TicketReply(BaseModel):
    response: str = Field(min_length=2, max_length=5000)


def faq_dict(f: FaqItem) -> dict:
    return {'id': f.id, 'category': f.category, 'question': f.question, 'answer': f.answer, 'sort': f.sort,
            'published': f.published, 'updated_at': iso(f.updated_at)}


def contact_dict(c: Contact) -> dict:
    return {'id': c.id, 'name': c.name, 'purpose': c.purpose, 'phone': c.phone, 'email': c.email, 'website': c.website,
            'address': c.address, 'sort': c.sort, 'published': c.published, 'updated_at': iso(c.updated_at)}


@app.get('/api/public/help')
def public_help(s: Session = Depends(db)):
    """Everything on the Help page. Open to everyone, including people who have not signed in yet."""
    inst = default_institution(s)
    c = content_map(s, inst.id)
    faqs = s.scalars(select(FaqItem).where(FaqItem.institution_id == inst.id, FaqItem.published == True)  # noqa: E712
                     .order_by(FaqItem.sort, FaqItem.question)).all()
    contacts = s.scalars(select(Contact).where(Contact.institution_id == inst.id, Contact.published == True)  # noqa: E712
                         .order_by(Contact.sort, Contact.name)).all()
    return {'content': {k: c[k] for k in PUBLIC_CONTENT}, 'support_categories': support_categories(s, inst.id),
            'faqs': [faq_dict(f) for f in faqs], 'contacts': [contact_dict(x) for x in contacts]}


@app.get('/api/admin/content')
def get_content(u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    return content_map(s, u.institution_id)


@app.put('/api/admin/content')
def set_content(req: dict, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    unknown = [k for k in req if k not in CONTENT_DEFAULTS]
    if unknown:
        raise HTTPException(422, 'Unknown setting: ' + ', '.join(unknown))
    clean = {}
    for k, v in req.items():
        if k == 'support_categories':
            cats = v if isinstance(v, list) else jl(str(v), None)
            ok = isinstance(cats, list) and 0 < len(cats) <= 20 and all(
                isinstance(x, dict) and re.fullmatch(r'[a-z][a-z0-9_]{1,30}', str(x.get('key', '')))
                and 2 <= len(str(x.get('label', '')).strip()) <= 120 and isinstance(x.get('days'), int) and 0 <= x['days'] <= 30
                for x in cats)
            if not ok or not isinstance(cats, list) or len({x['key'] for x in cats}) != len(cats):
                raise HTTPException(422, 'Each support category needs a unique key, a label and a reply time of 0-30 working days.')
            clean[k] = json.dumps([{'key': x['key'], 'label': str(x['label']).strip(), 'days': x['days']} for x in cats])
        else:
            text_v = str(v if v is not None else '').strip()
            if len(text_v) > 4000:
                raise HTTPException(422, f'{k} is too long (4000 characters at most).')
            if k == 'support_email' and text_v and not MSG.valid_email(text_v):
                raise HTTPException(422, 'Please enter a valid support email address.')
            clean[k] = text_v
    rows = {r.key: r for r in s.scalars(select(SiteContent).where(SiteContent.institution_id == u.institution_id)).all()}
    for k, v in clean.items():
        r = rows.get(k) or SiteContent(institution_id=u.institution_id, key=k)
        r.value, r.updated_by = v, u.id
        s.add(r)
    audit(s, u.institution_id, u.id, 'update_content', 'site_content', ','.join(sorted(clean)))
    s.commit()
    return content_map(s, u.institution_id)


@app.get('/api/form-fields')
def list_form_fields(include_disabled: bool = False, u: User = Depends(current_user), s: Session = Depends(db)):
    q = select(FormField).where(FormField.institution_id == u.institution_id)
    if not (include_disabled and u.role == 'admin'):
        q = q.where(FormField.enabled == True)  # noqa: E712
    return [field_dict(f) for f in s.scalars(q.order_by(FormField.sort, FormField.label)).all()]


def _check_field(req: FormFieldRequest) -> list[str]:
    if req.kind not in FIELD_KINDS:
        raise HTTPException(422, 'kind must be one of ' + ', '.join(FIELD_KINDS))
    opts = [o.strip() for o in req.options if o.strip()]
    if req.kind == 'select' and len(opts) < 2:
        raise HTTPException(422, 'A choice question needs at least two options.')
    if any(len(o) > 120 for o in opts):
        raise HTTPException(422, 'Options must be 120 characters or fewer.')
    return opts if req.kind == 'select' else []


@app.post('/api/form-fields')
def create_form_field(req: FormFieldRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    opts = _check_field(req)
    if req.key in IDEA_TEXT_FIELDS or s.scalar(select(FormField).where(FormField.institution_id == u.institution_id,
                                                                     FormField.key == req.key)):
        raise HTTPException(409, 'A question with that key already exists.')
    f = FormField(id=new_id(), institution_id=u.institution_id, key=req.key, label=req.label.strip(), help=req.help.strip(),
                  kind=req.kind, options_json=json.dumps(opts), required=req.required, sort=req.sort, enabled=req.enabled)
    s.add(f)
    audit(s, u.institution_id, u.id, 'create_form_field', 'form_field', f.id, json.dumps({'key': f.key, 'label': f.label}))
    s.commit()
    return field_dict(f)


@app.put('/api/form-fields/{field_id}')
def update_form_field(field_id: str, req: FormFieldRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    f = s.get(FormField, field_id)
    if not f or f.institution_id != u.institution_id:
        raise HTTPException(404, 'Question not found')
    if req.key != f.key:
        raise HTTPException(409, 'The key of a question cannot change, because saved answers use it. Add a new question instead.')
    opts = _check_field(req)
    f.label, f.help, f.kind, f.options_json = req.label.strip(), req.help.strip(), req.kind, json.dumps(opts)
    f.required, f.sort, f.enabled = req.required, req.sort, req.enabled
    audit(s, u.institution_id, u.id, 'update_form_field', 'form_field', f.id, json.dumps({'key': f.key, 'enabled': f.enabled}))
    s.commit()
    return field_dict(f)


def _crud_get(s: Session, model, item_id: str, u: User):
    x = s.get(model, item_id)
    if not x or x.institution_id != u.institution_id:
        raise HTTPException(404, 'Not found')
    return x


@app.get('/api/admin/faqs')
def admin_faqs(u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    return [faq_dict(f) for f in s.scalars(select(FaqItem).where(FaqItem.institution_id == u.institution_id)
                                           .order_by(FaqItem.sort, FaqItem.question)).all()]


@app.post('/api/faqs')
def create_faq(req: FaqRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    f = FaqItem(id=new_id(), institution_id=u.institution_id, **{k: (v.strip() if isinstance(v, str) else v) for k, v in req.model_dump().items()})
    s.add(f)
    audit(s, u.institution_id, u.id, 'create_faq', 'faq', f.id, f.question)
    s.commit()
    return faq_dict(f)


@app.put('/api/faqs/{faq_id}')
def update_faq(faq_id: str, req: FaqRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    f = _crud_get(s, FaqItem, faq_id, u)
    for k, v in req.model_dump().items():
        setattr(f, k, v.strip() if isinstance(v, str) else v)
    audit(s, u.institution_id, u.id, 'update_faq', 'faq', f.id, f.question)
    s.commit()
    return faq_dict(f)


@app.delete('/api/faqs/{faq_id}')
def delete_faq(faq_id: str, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    f = _crud_get(s, FaqItem, faq_id, u)
    audit(s, u.institution_id, u.id, 'delete_faq', 'faq', f.id, f.question)
    s.delete(f)
    s.commit()
    return {'deleted': faq_id}


@app.get('/api/admin/contacts')
def admin_contacts(u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    return [contact_dict(c) for c in s.scalars(select(Contact).where(Contact.institution_id == u.institution_id)
                                               .order_by(Contact.sort, Contact.name)).all()]


def _contact_values(req: ContactRequest) -> dict:
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in req.model_dump().items()}
    if d['email'] and not MSG.valid_email(d['email']):
        raise HTTPException(422, 'Please enter a valid email address.')
    if d['website'] and not re.match(r'^https?://[^\s]+$', d['website']):
        raise HTTPException(422, 'The website must start with http:// or https://')
    return d


@app.post('/api/contacts')
def create_contact(req: ContactRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    c = Contact(id=new_id(), institution_id=u.institution_id, **_contact_values(req))
    s.add(c)
    audit(s, u.institution_id, u.id, 'create_contact', 'contact', c.id, c.name)
    s.commit()
    return contact_dict(c)


@app.put('/api/contacts/{contact_id}')
def update_contact(contact_id: str, req: ContactRequest, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    c = _crud_get(s, Contact, contact_id, u)
    for k, v in _contact_values(req).items():
        setattr(c, k, v)
    audit(s, u.institution_id, u.id, 'update_contact', 'contact', c.id, c.name)
    s.commit()
    return contact_dict(c)


@app.delete('/api/contacts/{contact_id}')
def delete_contact(contact_id: str, u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    c = _crud_get(s, Contact, contact_id, u)
    audit(s, u.institution_id, u.id, 'delete_contact', 'contact', c.id, c.name)
    s.delete(c)
    s.commit()
    return {'deleted': contact_id}


def ticket_dict(s: Session, t: SupportTicket, cats: dict) -> dict:
    who = s.get(User, t.user_id)
    responder = s.get(User, t.responded_by) if t.responded_by else None
    return {'id': t.id, 'category': t.category, 'category_label': cats.get(t.category, t.category), 'subject': t.subject,
            'message': t.message, 'status': t.status, 'application_id': t.application_id,
            'created_at': iso(t.created_at), 'due_at': iso(t.due_at), 'overdue': t.status == 'open' and utcnow() > t.due_at,
            'response': t.response, 'responded_at': iso(t.responded_at),
            'responded_by': responder.full_name or responder.username if responder else None,
            'user': (who.full_name or who.username) if who else None}


@app.get('/api/support/tickets')
def list_tickets(status: str | None = None, u: User = Depends(current_user), s: Session = Depends(db)):
    """Submitters see their own requests; support staff see everyone's, oldest open first."""
    q = select(SupportTicket).where(SupportTicket.institution_id == u.institution_id)
    if u.role not in SUPPORT_ROLES:
        q = q.where(SupportTicket.user_id == u.id)
    if status:
        q = q.where(SupportTicket.status == status)
    rows = s.scalars(q.order_by(SupportTicket.created_at.desc()).limit(300)).all()
    cats = {c['key']: c['label'] for c in support_categories(s, u.institution_id)}
    items = [ticket_dict(s, t, cats) for t in rows]
    out: dict = {'items': items, 'categories': support_categories(s, u.institution_id)}
    if u.role in SUPPORT_ROLES:
        answered = [t for t in rows if t.responded_at]
        out['stats'] = {'open': sum(1 for t in rows if t.status == 'open'), 'overdue': sum(1 for i in items if i['overdue']),
                        'answered': len(answered),
                        'answered_on_time': sum(1 for t in answered if t.responded_at <= t.due_at)}
    return out


@app.post('/api/support/tickets')
def create_ticket(req: TicketRequest, u: User = Depends(current_user), s: Session = Depends(db)):
    if not rate_ok(f'ticket:{u.id}', 10, 3600):
        raise HTTPException(429, 'You have sent a lot of requests in the last hour. Please wait a little.')
    cats = {c['key']: c for c in support_categories(s, u.institution_id)}
    cat = cats.get(req.category)
    if not cat:
        raise HTTPException(422, 'Please choose what your question is about.')
    if req.application_id:
        get_app_or_404(s, req.application_id, u)
    now = utcnow()
    t = SupportTicket(id=new_id(), institution_id=u.institution_id, user_id=u.id, application_id=req.application_id,
                      category=req.category, subject=req.subject.strip(), message=req.message.strip(),
                      due_at=add_working_days(now, cat['days']) if cat['days'] else now + timedelta(hours=4), created_at=now)
    s.add(t)
    for sid in staff_ids(s, u.institution_id, SUPPORT_ROLES, u.id):
        notify_user(s, u.institution_id, sid, 'support', f'New support request: {t.subject}', 'New support request')
    audit(s, u.institution_id, u.id, 'create_ticket', 'support_ticket', t.id, t.subject)
    s.commit()
    return ticket_dict(s, t, {k: v['label'] for k, v in cats.items()})


@app.post('/api/support/tickets/{ticket_id}/reply')
def reply_ticket(ticket_id: str, req: TicketReply, u: User = Depends(require_roles(*SUPPORT_ROLES)), s: Session = Depends(db)):
    t = _crud_get(s, SupportTicket, ticket_id, u)
    if t.status == 'closed':
        raise HTTPException(409, 'This request is closed.')
    t.response, t.responded_by, t.responded_at, t.status = req.response.strip(), u.id, utcnow(), 'answered'
    notify_user(s, u.institution_id, t.user_id, 'support', f'We replied to your support request "{t.subject}": {t.response[:400]}',
                'Reply to your support request')
    audit(s, u.institution_id, u.id, 'reply_ticket', 'support_ticket', t.id)
    s.commit()
    cats = {c['key']: c['label'] for c in support_categories(s, u.institution_id)}
    return ticket_dict(s, t, cats)


@app.post('/api/support/tickets/{ticket_id}/close')
def close_ticket(ticket_id: str, u: User = Depends(current_user), s: Session = Depends(db)):
    t = _crud_get(s, SupportTicket, ticket_id, u)
    if t.user_id != u.id and u.role not in SUPPORT_ROLES:
        raise HTTPException(404, 'Not found')
    t.status = 'closed'
    audit(s, u.institution_id, u.id, 'close_ticket', 'support_ticket', t.id)
    s.commit()
    cats = {c['key']: c['label'] for c in support_categories(s, u.institution_id)}
    return ticket_dict(s, t, cats)


@app.get('/api/audit')
def audit_log(limit: int = Query(200, ge=1, le=1000), u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    names = {x.id: x.username for x in s.scalars(select(User).where(User.institution_id == u.institution_id)).all()}
    rows = s.scalars(select(AuditLog).where(AuditLog.institution_id == u.institution_id).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{'id': x.id, 'user': names.get(x.user_id, x.user_id), 'action': x.action, 'entity_type': x.entity_type,
             'entity_id': x.entity_id, 'detail': x.detail, 'created_at': iso(x.created_at)} for x in rows]


@app.get('/api/audit/verify')
def audit_verify(u: User = Depends(require_roles('admin')), s: Session = Depends(db)):
    rows = s.scalars(select(AuditLog).where(AuditLog.institution_id == u.institution_id).order_by(AuditLog.id)).all()
    return verify_audit_chain(rows)


def _counts(s: Session, col, inst_id: str) -> dict:
    return {k: v for k, v in s.execute(select(col, func.count()).where(Application.institution_id == inst_id).group_by(col)).all()}


@app.get('/api/management/dashboard')
def management_dashboard(u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    inst = u.institution_id
    status = _counts(s, Application.status, inst)
    tot = s.execute(select(func.count(), func.coalesce(func.sum(Application.funding_request), 0),
                           func.coalesce(func.sum(Application.high_risks), 0), func.coalesce(func.sum(Application.medium_risks), 0),
                           func.coalesce(func.sum(Application.low_risks), 0), func.avg(Application.coverage_pct))
                    .where(Application.institution_id == inst)).one()
    open_dd = s.scalar(select(func.count()).select_from(DueDiligenceQuestion).where(
        DueDiligenceQuestion.institution_id == inst, DueDiligenceQuestion.status == 'open'))
    knowledge = s.scalar(select(func.count()).select_from(KnowledgeItem).where(
        KnowledgeItem.institution_id == inst, KnowledgeItem.active == True))  # noqa: E712
    return {'version': VERSION, 'applications': tot[0], 'funding_requested': float(tot[1]), 'status': status,
            'risk_counts': {'high': int(tot[2]), 'medium': int(tot[3]), 'low': int(tot[4])},
            'average_coverage_pct': round(tot[5], 1) if tot[5] is not None else None,
            'open_due_diligence': open_dd, 'knowledge_items': knowledge}


@app.get('/api/pilot/summary')
def pilot_summary(u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    inst = u.institution_id
    status = _counts(s, Application.status, inst)
    n = sum(status.values())
    assessed = s.scalar(select(func.count()).select_from(Application).where(Application.institution_id == inst, Application.latest_version > 0))
    reviewed = sum(status.get(k, 0) for k in ('under_review', 'request_information', 'refer_committee', 'approve', 'decline'))
    decided = status.get('approve', 0) + status.get('decline', 0)
    dd = dict(s.execute(select(DueDiligenceQuestion.status, func.count()).where(DueDiligenceQuestion.institution_id == inst)
                        .group_by(DueDiligenceQuestion.status)).all())
    live = dd.get('open', 0) + dd.get('answered', 0)
    pct = lambda x, y: round(x / y * 100, 1) if y else 0  # noqa: E731
    return {'version': VERSION, 'applications': n, 'assessments_completed': assessed, 'reviewed': reviewed,
            'committee_referrals': status.get('refer_committee', 0), 'final_decisions': decided,
            'due_diligence_answer_rate': pct(dd.get('answered', 0), live),
            'pilot_metrics': {'assessment_completion_rate': pct(assessed, n), 'review_coverage_rate': pct(reviewed, n),
                              'decision_coverage_rate': pct(decided, n)},
            'human_decision_required': True}


# Feature modules. Imported here, after the models and helpers they use are defined.
from . import reports  # noqa: E402

app.include_router(reports.router)

# Static frontend is mounted last so /api/* routes take precedence.
app.mount('/', StaticFiles(directory=ROOT / 'frontend', html=True), name='frontend')
