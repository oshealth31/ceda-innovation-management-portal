"""Reports for managers: who is submitting, what happens to their ideas, and how long each step takes.

Every figure is computed from the live database for the signed-in user's institution. Personal details
(gender, year of birth, district) are optional at sign-up and only ever shown as counts here.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .app import (STAFF, Application, DecisionRecord, Document, FormField, FundingProgram, ReviewScore,
                  SupportTicket, User, db, iso, jl, require_roles, utcnow)

router = APIRouter(prefix='/api/reports', tags=['reports'])

STATUS_LABEL = {'draft': 'Draft', 'under_review': 'Under review', 'request_information': 'Sent back for information',
                'refer_committee': 'With adjudicators', 'approve': 'Approved', 'decline': 'Declined'}
GENDER_LABEL = {'female': 'Female', 'male': 'Male', 'other': 'Other', 'prefer_not': 'Prefer not to say'}
NOT_STATED = 'Not stated'
AGE_BANDS = ((18, 24, '18-24'), (25, 29, '25-29'), (30, 35, '30-35'), (36, 45, '36-45'), (46, 200, '46 and over'))
FINAL = ('approve', 'decline')


def age_band(birth_year: int | None, today: date | None = None) -> str:
    if not birth_year:
        return NOT_STATED
    age = (today or date.today()).year - birth_year
    for lo, hi, label in AGE_BANDS:
        if lo <= age <= hi:
            return label
    return 'Under 18'


def is_youth(birth_year: int | None, today: date | None = None) -> bool | None:
    """CEDA's youth definition: 18 to 35."""
    if not birth_year:
        return None
    return 18 <= (today or date.today()).year - birth_year <= 35


def _days(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 86400, 1) if a and b and b >= a else None


def _avg(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _parse_day(v: str | None, end: bool = False) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.strptime(v, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(422, 'Dates must look like 2026-11-30.')
    return d.replace(hour=23, minute=59, second=59) if end else d


def collect(s: Session, inst_id: str, start: str | None, end: str | None, programme_id: str | None) -> list[dict]:
    """One row per idea with everything the reports need. Shared by the summary and the CSV download."""
    q = select(Application).where(Application.institution_id == inst_id)
    lo, hi = _parse_day(start), _parse_day(end, end=True)
    if lo:
        q = q.where(Application.created_at >= lo)
    if hi:
        q = q.where(Application.created_at <= hi)
    if programme_id:
        q = q.where(Application.programme_id == programme_id)
    apps = s.scalars(q.order_by(Application.created_at)).all()
    if not apps:
        return []
    ids = [a.id for a in apps]
    users = {u.id: u for u in s.scalars(select(User).where(User.id.in_({a.created_by for a in apps}))).all()}
    progs = {p.id: p.name for p in s.scalars(select(FundingProgram).where(FundingProgram.institution_id == inst_id)).all()}
    first_doc: dict[str, datetime] = {}
    for app_id, at in s.execute(select(Document.application_id, Document.created_at).where(Document.application_id.in_(ids))).all():
        if app_id not in first_doc or at < first_doc[app_id]:
            first_doc[app_id] = at
    decisions = defaultdict(list)
    for d in s.scalars(select(DecisionRecord).where(DecisionRecord.application_id.in_(ids)).order_by(DecisionRecord.created_at)).all():
        decisions[d.application_id].append(d)
    scores = defaultdict(list)
    for app_id, sc in s.execute(select(ReviewScore.application_id, ReviewScore.score).where(ReviewScore.application_id.in_(ids))).all():
        scores[app_id].append(sc)
    rows = []
    for a in apps:
        owner = users.get(a.created_by)
        own = owner if owner and owner.role == 'applicant' else None  # staff-entered ideas have no submitter profile
        submitted = first_doc.get(a.id)
        ds = decisions.get(a.id, [])
        after = [d for d in ds if submitted and d.created_at >= submitted]
        first_review = after[0].created_at if after else None
        referred = next((d.created_at for d in after if d.decision == 'refer_committee'), None)
        final = next((d for d in reversed(ds) if d.decision in FINAL), None)
        rows.append({
            'id': a.id, 'title': a.idea_title or a.applicant_name, 'submitter': a.applicant_name,
            'programme': progs.get(a.programme_id or '', NOT_STATED) if a.programme_id else NOT_STATED,
            'sector': a.sector or NOT_STATED, 'stage': a.stage or NOT_STATED, 'status': a.status,
            'gender': GENDER_LABEL.get(own.gender, NOT_STATED) if own and own.gender else NOT_STATED,
            'age_band': age_band(own.birth_year) if own else NOT_STATED,
            'youth': is_youth(own.birth_year) if own else None,
            'district': (own.district if own and own.district else NOT_STATED),
            'funding_request': float(a.funding_request or 0),
            'created_at': a.created_at, 'submitted_at': submitted, 'first_review_at': first_review,
            'referred_at': referred, 'decided_at': final.created_at if final else None,
            'days_to_first_review': _days(submitted, first_review),
            'days_in_evaluation': _days(submitted, referred),
            'days_in_adjudication': _days(referred, final.created_at if final else None),
            'days_to_decision': _days(submitted, final.created_at if final else None),
            'times_sent_back': sum(1 for d in ds if d.decision == 'request_information'),
            'average_score': _avg(scores.get(a.id, [])),
            'answers': jl(a.extra_json, {}),
        })
    return rows


def breakdown(rows: list[dict], key: str, order: tuple | None = None) -> list[dict]:
    """Ideas per group, with how many were approved and declined."""
    total = Counter(r[key] for r in rows)
    appr = Counter(r[key] for r in rows if r['status'] == 'approve')
    decl = Counter(r[key] for r in rows if r['status'] == 'decline')
    labels = [x for x in (order or ()) if x in total] + sorted(k for k in total if not order or k not in order)
    if NOT_STATED in labels:  # always last
        labels.remove(NOT_STATED)
        labels.append(NOT_STATED)
    out = []
    for k in labels:
        decided = appr[k] + decl[k]
        out.append({'label': k, 'count': total[k], 'approved': appr[k], 'declined': decl[k],
                    'approval_rate': round(appr[k] / decided * 100, 1) if decided else None})
    return out


@router.get('/summary')
def summary(start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'),
            programme_id: str | None = None, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    rows = collect(s, u.institution_id, start, end, programme_id)
    submitted = [r for r in rows if r['submitted_at']]
    approved = [r for r in rows if r['status'] == 'approve']
    declined = [r for r in rows if r['status'] == 'decline']
    decided = len(approved) + len(declined)
    youth_known = [r for r in rows if r['youth'] is not None]
    by_month = Counter(r['created_at'].strftime('%Y-%m') for r in rows)
    tickets = s.scalars(select(SupportTicket).where(SupportTicket.institution_id == u.institution_id)).all()
    answered = [t for t in tickets if t.responded_at]
    now = utcnow()
    return {
        'filters': {'from': start, 'to': end, 'programme_id': programme_id},
        'generated_at': iso(now),
        'totals': {
            'ideas': len(rows), 'submitted': len(submitted), 'in_progress': sum(1 for r in rows if r['status'] not in FINAL + ('draft',)),
            'drafts': sum(1 for r in rows if r['status'] == 'draft'),
            'approved': len(approved), 'declined': len(declined),
            'approval_rate': round(len(approved) / decided * 100, 1) if decided else None,
            'funding_requested': round(sum(r['funding_request'] for r in rows), 2),
            'funding_requested_approved': round(sum(r['funding_request'] for r in approved), 2),
            'youth_share': round(sum(1 for r in youth_known if r['youth']) / len(youth_known) * 100, 1) if youth_known else None,
            'women_share': (round(sum(1 for r in rows if r['gender'] == 'Female') / len([r for r in rows if r['gender'] != NOT_STATED]) * 100, 1)
                            if any(r['gender'] != NOT_STATED for r in rows) else None),
        },
        'by_status': [{'label': STATUS_LABEL.get(k, k), 'key': k, 'count': n}
                      for k, n in sorted(Counter(r['status'] for r in rows).items(), key=lambda x: list(STATUS_LABEL).index(x[0]) if x[0] in STATUS_LABEL else 99)],
        'by_sector': breakdown(rows, 'sector'),
        'by_stage': breakdown(rows, 'stage'),
        'by_district': breakdown(rows, 'district'),
        'by_gender': breakdown(rows, 'gender', tuple(GENDER_LABEL.values())),
        'by_age': breakdown(rows, 'age_band', tuple(b[2] for b in AGE_BANDS)),
        'by_programme': breakdown(rows, 'programme'),
        'by_month': [{'label': k, 'count': by_month[k]} for k in sorted(by_month)],
        'timing_days': {
            'to_first_review': _avg([r['days_to_first_review'] for r in rows]),
            'in_evaluation': _avg([r['days_in_evaluation'] for r in rows]),
            'in_adjudication': _avg([r['days_in_adjudication'] for r in rows]),
            'to_decision': _avg([r['days_to_decision'] for r in rows]),
            'waiting_over_30_days': sum(1 for r in submitted if r['status'] not in FINAL and (now - r['submitted_at']).days > 30),
        },
        'sent_back_share': round(sum(1 for r in submitted if r['times_sent_back']) / len(submitted) * 100, 1) if submitted else None,
        'average_score': _avg([r['average_score'] for r in rows]),
        'support': {'requests': len(tickets), 'open': sum(1 for t in tickets if t.status == 'open'),
                    'answered_on_time_pct': round(sum(1 for t in answered if t.responded_at <= t.due_at) / len(answered) * 100, 1) if answered else None},
    }


CSV_COLUMNS = [('title', 'Idea'), ('submitter', 'Submitter'), ('programme', 'Call / programme'), ('sector', 'Sector'),
               ('stage', 'Stage'), ('status_label', 'Status'), ('gender', 'Gender'), ('age_band', 'Age group'),
               ('youth_label', 'Youth (18-35)'), ('district', 'District'), ('funding_request', 'Funding requested (BWP)'),
               ('created', 'Created'), ('submitted', 'Submitted'), ('decided', 'Decided'),
               ('days_to_first_review', 'Days to first review'), ('days_in_evaluation', 'Days in evaluation'),
               ('days_in_adjudication', 'Days in adjudication'), ('days_to_decision', 'Days to decision'),
               ('times_sent_back', 'Times sent back'), ('average_score', 'Average score')]


def _cell(v) -> str:
    """Stop spreadsheet programs treating text from users as formulas."""
    t = '' if v is None else str(v)
    return "'" + t if t[:1] in ('=', '+', '-', '@', '\t', '\r') else t


@router.get('/ideas.csv')
def ideas_csv(start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'),
              programme_id: str | None = None, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    """Every idea in the filter as a spreadsheet, one row each, including answers to extra form questions."""
    rows = collect(s, u.institution_id, start, end, programme_id)
    extra = s.scalars(select(FormField).where(FormField.institution_id == u.institution_id).order_by(FormField.sort, FormField.label)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _, h in CSV_COLUMNS] + [f.label for f in extra])
    day = lambda d: d.strftime('%Y-%m-%d') if d else ''  # noqa: E731
    for r in rows:
        r = {**r, 'status_label': STATUS_LABEL.get(r['status'], r['status']),
             'youth_label': {True: 'Yes', False: 'No', None: NOT_STATED}[r['youth']],
             'created': day(r['created_at']), 'submitted': day(r['submitted_at']), 'decided': day(r['decided_at'])}
        ans = r['answers']
        fmt = lambda v: 'Yes' if v is True else 'No' if v is False else v  # noqa: E731
        w.writerow([_cell(r[k]) for k, _ in CSV_COLUMNS] + [_cell(fmt(ans.get(f.key))) for f in extra])
    name = f"ceda-ideas-{utcnow().strftime('%Y%m%d')}.csv"
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


@router.get('/breakdowns.csv')
def breakdowns_csv(start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'),
                   programme_id: str | None = None, u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    """The summary tables in one sheet: one block per breakdown."""
    d = summary(start, end, programme_id, u, s)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Report generated', d['generated_at'], 'From', start or 'start', 'To', end or 'today'])
    for title, key in (('Sector', 'by_sector'), ('Stage', 'by_stage'), ('District', 'by_district'), ('Gender', 'by_gender'),
                       ('Age group', 'by_age'), ('Call / programme', 'by_programme')):
        w.writerow([])
        w.writerow([title, 'Ideas', 'Approved', 'Declined', 'Approval rate %'])
        for x in d[key]:
            w.writerow([_cell(x['label']), x['count'], x['approved'], x['declined'], '' if x['approval_rate'] is None else x['approval_rate']])
    w.writerow([])
    w.writerow(['Average days', 'Value'])
    for k, v in d['timing_days'].items():
        w.writerow([k.replace('_', ' '), '' if v is None else v])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="ceda-report-{utcnow().strftime("%Y%m%d")}.csv"'})
