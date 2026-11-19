"""Live chat between signed-in users and the institution's chat consultants.

How it works
- The administrator marks staff accounts as chat consultants. A consultant counts as online while their chat
  console is open (it checks in every few seconds).
- Chat is open on working days between the configured hours (Botswana time, 09:00-16:00 by default) when at least
  one consultant is online. Otherwise people see the away message and can leave a message, which becomes a support
  request with its normal reply time.
- A new chat waits in a shared queue. Any consultant can take it; it can be handed to another consultant.
- A chat nobody takes within CHAT_MISSED_MINUTES is marked missed and turned into a support request automatically,
  so no question is lost.
- Messages are fetched by polling (every few seconds), which works through every proxy and on every phone.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .app import (STAFF, Base, Notification, SupportTicket, User, add_working_days, audit, content_map, current_user, db,
                  iso, new_id, rate_ok, require_roles, support_categories, utcnow)

router = APIRouter(prefix='/api/chat', tags=['chat'])

UTC_OFFSET_HOURS = float(os.getenv('CHAT_UTC_OFFSET_HOURS', '2'))  # Botswana is UTC+2 all year
ONLINE_SECONDS = 45            # a consultant is online if their console checked in this recently
MISSED_MINUTES = int(os.getenv('CHAT_MISSED_MINUTES', '10'))
OPEN_STATES = ('waiting', 'active')


class ChatSession(Base):
    __tablename__ = 'chat_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey('institutions.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    topic: Mapped[str] = mapped_column(String(200), default='')
    status: Mapped[str] = mapped_column(String(20), default='waiting', index=True)  # waiting|active|closed|missed
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatMessage(Base):
    __tablename__ = 'chat_messages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('chat_sessions.id'), index=True)
    sender: Mapped[str] = mapped_column(String(10))  # user|agent|system
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StartChat(BaseModel):
    topic: str = Field(default='', max_length=200)
    message: str = Field(min_length=2, max_length=2000)


class SendMessage(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class Transfer(BaseModel):
    agent_id: int


class Rating(BaseModel):
    rating: int = Field(ge=1, le=5)


class LeaveMessage(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=10, max_length=5000)


# ---------- helpers ----------
def local_now(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(hours=UTC_OFFSET_HOURS)


def is_agent(u: User) -> bool:
    return bool(u.chat_agent and u.role in STAFF and u.active)


def require_agent(u: User = Depends(current_user)) -> User:
    if not (is_agent(u) or u.role == 'admin'):
        raise HTTPException(403, 'Only chat consultants can do this.')
    return u


def agents_online(s: Session, inst_id: str, now: datetime | None = None) -> list[User]:
    cutoff = (now or utcnow()) - timedelta(seconds=ONLINE_SECONDS)
    rows = s.scalars(select(User).where(User.institution_id == inst_id, User.chat_agent == True,  # noqa: E712
                                        User.active == True, User.chat_seen_at >= cutoff)).all()  # noqa: E712
    return [a for a in rows if a.role in STAFF]


def hours_state(c: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Is it inside chat hours? Returns (open, reason)."""
    t = local_now(now)
    if t.weekday() >= 5:
        return False, 'weekend'
    if t.strftime('%Y-%m-%d') in set((c.get('chat_closed_dates') or '').split()):
        return False, 'public holiday'
    hm = t.strftime('%H:%M')
    if not (c['chat_open'] <= hm < c['chat_close']):
        return False, 'outside hours'
    return True, ''


def name_of(u: User | None) -> str:
    return (u.full_name or u.username) if u else 'Someone'


def add_msg(s: Session, cs: ChatSession, sender: str, body: str, sender_id: int | None = None) -> ChatMessage:
    m = ChatMessage(session_id=cs.id, sender=sender, sender_id=sender_id, body=body, created_at=utcnow())
    s.add(m)
    cs.last_message_at = m.created_at
    return m


def sweep_missed(s: Session, inst_id: str, now: datetime | None = None) -> int:
    """Chats nobody took in time become support requests, so the person still gets an answer."""
    now = now or utcnow()
    late = s.scalars(select(ChatSession).where(ChatSession.institution_id == inst_id, ChatSession.status == 'waiting',
                                               ChatSession.created_at < now - timedelta(minutes=MISSED_MINUTES))).all()
    if not late:
        return 0
    cats = support_categories(s, inst_id)
    cat = next((c for c in cats if c['key'] == 'other'), cats[-1])
    for cs in late:
        first = s.scalar(select(ChatMessage).where(ChatMessage.session_id == cs.id, ChatMessage.sender == 'user')
                         .order_by(ChatMessage.id))
        t = SupportTicket(id=new_id(), institution_id=inst_id, user_id=cs.user_id, category=cat['key'],
                          subject=('Chat: ' + (cs.topic or 'question'))[:200],
                          message=(first.body if first else 'Question left in live chat.')[:5000],
                          due_at=add_working_days(now, cat['days']) if cat['days'] else now + timedelta(hours=4), created_at=now)
        s.add(t)
        cs.status, cs.closed_at, cs.ticket_id = 'missed', now, t.id
        add_msg(s, cs, 'system', 'Sorry, no consultant was free to answer. We have turned your question into a support '
                                 'request, and you will find the reply on the Support page.')
    s.commit()
    return len(late)


def session_or_404(s: Session, sid: str, u: User) -> ChatSession:
    cs = s.get(ChatSession, sid)
    if not cs or cs.institution_id != u.institution_id:
        raise HTTPException(404, 'Chat not found')
    if cs.user_id != u.id and not (is_agent(u) or u.role == 'admin'):
        raise HTTPException(404, 'Chat not found')
    return cs


def session_dict(s: Session, cs: ChatSession, viewer: User, after: int = 0) -> dict:
    msgs = s.scalars(select(ChatMessage).where(ChatMessage.session_id == cs.id, ChatMessage.id > after)
                     .order_by(ChatMessage.id)).all()
    names = {u.id: name_of(u) for u in s.scalars(select(User).where(User.id.in_({m.sender_id for m in msgs if m.sender_id}
                                                                                     | {cs.user_id, cs.agent_id or 0}))).all()}
    position = None
    if cs.status == 'waiting':
        position = 1 + len(s.scalars(select(ChatSession.id).where(ChatSession.institution_id == cs.institution_id,
                                                                    ChatSession.status == 'waiting',
                                                                    ChatSession.created_at < cs.created_at)).all())
    return {'id': cs.id, 'status': cs.status, 'topic': cs.topic, 'user': names.get(cs.user_id), 'user_id': cs.user_id,
            'agent': names.get(cs.agent_id) if cs.agent_id else None, 'agent_id': cs.agent_id, 'mine': cs.agent_id == viewer.id,
            'queue_position': position, 'rating': cs.rating, 'ticket_id': cs.ticket_id,
            'created_at': iso(cs.created_at), 'accepted_at': iso(cs.accepted_at), 'closed_at': iso(cs.closed_at),
            'messages': [{'id': m.id, 'sender': m.sender, 'name': names.get(m.sender_id) if m.sender_id else None,
                          'body': m.body, 'at': iso(m.created_at), 'own': m.sender_id == viewer.id} for m in msgs]}


# ---------- for everyone ----------
@router.get('/status')
def chat_status(u: User = Depends(current_user), s: Session = Depends(db)):
    c = content_map(s, u.institution_id)
    in_hours, reason = hours_state(c)
    online = len(agents_online(s, u.institution_id))
    open_now = in_hours and online > 0
    if in_hours and not online:
        reason = 'no consultant online'
    mine = s.scalar(select(ChatSession).where(ChatSession.user_id == u.id, ChatSession.status.in_(OPEN_STATES))
                    .order_by(ChatSession.created_at.desc()))
    return {'open': open_now, 'reason': reason, 'hours': f"Monday to Friday, {c['chat_open']} to {c['chat_close']}",
            'agents_online': online, 'away_message': c['chat_away_message'], 'active_session_id': mine.id if mine else None,
            'is_agent': is_agent(u)}


@router.post('/sessions')
def start_chat(req: StartChat, u: User = Depends(current_user), s: Session = Depends(db)):
    sweep_missed(s, u.institution_id)
    existing = s.scalar(select(ChatSession).where(ChatSession.user_id == u.id, ChatSession.status.in_(OPEN_STATES)))
    if existing:
        return session_dict(s, existing, u)
    c = content_map(s, u.institution_id)
    in_hours, reason = hours_state(c)
    agents = agents_online(s, u.institution_id)
    if not in_hours or not agents:
        raise HTTPException(409, {'message': c['chat_away_message'], 'reason': reason or 'no consultant online', 'away': True})
    if not rate_ok(f'chatstart:{u.id}', 5, 3600):
        raise HTTPException(429, 'You have started a lot of chats in the last hour. Please wait a little.')
    cs = ChatSession(id=new_id(), institution_id=u.institution_id, user_id=u.id, topic=req.topic.strip(), created_at=utcnow())
    s.add(cs)
    s.flush()
    add_msg(s, cs, 'user', req.message.strip(), u.id)
    add_msg(s, cs, 'system', 'Thanks. A consultant will be with you shortly. Please keep this window open.')
    for a in agents:
        s.add(Notification(institution_id=u.institution_id, user_id=a.id, kind='chat',
                           message=f'New chat from {name_of(u)}: {cs.topic or req.message[:80]}'))
    audit(s, u.institution_id, u.id, 'chat_start', 'chat', cs.id)
    s.commit()
    return session_dict(s, cs, u)


@router.post('/leave-message')
def leave_message(req: LeaveMessage, u: User = Depends(current_user), s: Session = Depends(db)):
    """Outside chat hours: the message becomes a support request with the normal reply time."""
    if not rate_ok(f'ticket:{u.id}', 10, 3600):
        raise HTTPException(429, 'You have sent a lot of requests in the last hour. Please wait a little.')
    cats = support_categories(s, u.institution_id)
    cat = next((c for c in cats if c['key'] == 'other'), cats[-1])
    now = utcnow()
    t = SupportTicket(id=new_id(), institution_id=u.institution_id, user_id=u.id, category=cat['key'],
                      subject=req.subject.strip(), message=req.message.strip(),
                      due_at=add_working_days(now, cat['days']) if cat['days'] else now + timedelta(hours=4), created_at=now)
    s.add(t)
    audit(s, u.institution_id, u.id, 'chat_leave_message', 'support_ticket', t.id)
    s.commit()
    return {'ticket_id': t.id, 'due_at': iso(t.due_at), 'category': cat['label']}


@router.get('/sessions/{sid}')
def get_session(sid: str, after: int = 0, u: User = Depends(current_user), s: Session = Depends(db)):
    """Polled every few seconds by both sides: new messages since `after`, plus the chat's state."""
    sweep_missed(s, u.institution_id)
    cs = session_or_404(s, sid, u)
    if is_agent(u):
        u.chat_seen_at = utcnow()
        s.commit()
    return session_dict(s, cs, u, after)


@router.post('/sessions/{sid}/messages')
def send_message(sid: str, req: SendMessage, u: User = Depends(current_user), s: Session = Depends(db)):
    cs = session_or_404(s, sid, u)
    if cs.status not in OPEN_STATES:
        raise HTTPException(409, 'This chat has ended.')
    if not rate_ok(f'chatmsg:{u.id}', 40, 60):
        raise HTTPException(429, 'Slow down a little, please.')
    if u.id == cs.user_id:
        sender = 'user'
    elif cs.agent_id == u.id:
        sender = 'agent'
        if not cs.first_reply_at:
            cs.first_reply_at = utcnow()
    else:
        raise HTTPException(409, 'Take this chat first, or ask the consultant handling it to hand it over.')
    m = add_msg(s, cs, sender, req.body.strip(), u.id)
    s.commit()
    return {'id': m.id, 'at': iso(m.created_at)}


@router.post('/sessions/{sid}/close')
def close_chat(sid: str, u: User = Depends(current_user), s: Session = Depends(db)):
    cs = session_or_404(s, sid, u)
    if cs.status not in OPEN_STATES:
        return session_dict(s, cs, u)
    if u.id != cs.user_id and cs.agent_id not in (None, u.id) and u.role != 'admin':
        raise HTTPException(409, 'Only the consultant handling this chat can end it.')
    cs.status, cs.closed_at = 'closed', utcnow()
    add_msg(s, cs, 'system', f'{name_of(u)} ended the chat.')
    audit(s, u.institution_id, u.id, 'chat_close', 'chat', cs.id)
    s.commit()
    return session_dict(s, cs, u)


@router.post('/sessions/{sid}/rate')
def rate_chat(sid: str, req: Rating, u: User = Depends(current_user), s: Session = Depends(db)):
    cs = session_or_404(s, sid, u)
    if cs.user_id != u.id:
        raise HTTPException(403, 'Only the person who asked can rate the chat.')
    if cs.status != 'closed' or not cs.agent_id:
        raise HTTPException(409, 'You can rate a chat once a consultant has helped you and it has ended.')
    cs.rating = req.rating
    s.commit()
    return {'rating': cs.rating}


@router.get('/history')
def my_history(u: User = Depends(current_user), s: Session = Depends(db)):
    rows = s.scalars(select(ChatSession).where(ChatSession.user_id == u.id).order_by(ChatSession.created_at.desc()).limit(20)).all()
    return [{'id': x.id, 'topic': x.topic, 'status': x.status, 'created_at': iso(x.created_at), 'rating': x.rating} for x in rows]


# ---------- consultants ----------
@router.get('/queue')
def queue(u: User = Depends(require_agent), s: Session = Depends(db)):
    """The consultant console. Calling it marks the consultant as online."""
    if is_agent(u):
        u.chat_seen_at = utcnow()
        s.commit()
    sweep_missed(s, u.institution_id)
    rows = s.scalars(select(ChatSession).where(ChatSession.institution_id == u.institution_id, ChatSession.status.in_(OPEN_STATES))
                     .order_by(ChatSession.created_at)).all()
    users = {x.id: x for x in s.scalars(select(User).where(User.id.in_({r.user_id for r in rows} | {r.agent_id or 0 for r in rows}))).all()}
    now = utcnow()
    last = {}
    for r in rows:
        m = s.scalar(select(ChatMessage).where(ChatMessage.session_id == r.id, ChatMessage.sender != 'system').order_by(ChatMessage.id.desc()))
        last[r.id] = m
    item = lambda r: {'id': r.id, 'topic': r.topic, 'status': r.status, 'user': name_of(users.get(r.user_id)),  # noqa: E731
                      'agent': name_of(users.get(r.agent_id)) if r.agent_id else None, 'mine': r.agent_id == u.id,
                      'waiting_seconds': int((now - r.created_at).total_seconds()),
                      'last_message': last[r.id].body[:140] if last[r.id] else '',
                      'last_from_user': bool(last[r.id] and last[r.id].sender == 'user'),
                      'created_at': iso(r.created_at)}
    c = content_map(s, u.institution_id)
    in_hours, reason = hours_state(c)
    online = agents_online(s, u.institution_id)
    return {'waiting': [item(r) for r in rows if r.status == 'waiting'],
            'mine': [item(r) for r in rows if r.status == 'active' and r.agent_id == u.id],
            'others': [item(r) for r in rows if r.status == 'active' and r.agent_id != u.id],
            'online': [{'id': a.id, 'name': name_of(a)} for a in online], 'in_hours': in_hours, 'reason': reason,
            'me_agent': is_agent(u)}


@router.post('/offline')
def go_offline(u: User = Depends(require_agent), s: Session = Depends(db)):
    u.chat_seen_at = None
    s.commit()
    return {'online': False}


@router.post('/sessions/{sid}/claim')
def claim(sid: str, u: User = Depends(require_agent), s: Session = Depends(db)):
    if not is_agent(u):
        raise HTTPException(403, 'Mark your account as a chat consultant first (Users page).')
    cs = session_or_404(s, sid, u)
    if cs.status != 'waiting':
        raise HTTPException(409, f'{"Another consultant has already taken this chat." if cs.status == "active" else "This chat has ended."}')
    now = utcnow()
    cs.status, cs.agent_id, cs.accepted_at = 'active', u.id, now
    add_msg(s, cs, 'system', f'{name_of(u)} has joined the chat.')
    audit(s, u.institution_id, u.id, 'chat_claim', 'chat', cs.id)
    s.commit()
    return session_dict(s, cs, u)


@router.post('/sessions/{sid}/transfer')
def transfer(sid: str, req: Transfer, u: User = Depends(require_agent), s: Session = Depends(db)):
    cs = session_or_404(s, sid, u)
    if cs.status != 'active' or (cs.agent_id != u.id and u.role != 'admin'):
        raise HTTPException(409, 'Only the consultant handling an active chat can hand it over.')
    to = s.get(User, req.agent_id)
    if not to or to.institution_id != u.institution_id or not is_agent(to):
        raise HTTPException(422, 'Choose another chat consultant.')
    if to.id == cs.agent_id:
        raise HTTPException(422, 'That consultant already has this chat.')
    cs.agent_id = to.id
    add_msg(s, cs, 'system', f'{name_of(u)} handed the chat to {name_of(to)}.')
    s.add(Notification(institution_id=u.institution_id, user_id=to.id, kind='chat', message=f'{name_of(u)} handed you a chat: {cs.topic}'))
    audit(s, u.institution_id, u.id, 'chat_transfer', 'chat', cs.id, f'to={to.id}')
    s.commit()
    return session_dict(s, cs, u)


@router.get('/agents')
def list_agents(u: User = Depends(require_agent), s: Session = Depends(db)):
    online = {a.id for a in agents_online(s, u.institution_id)}
    rows = s.scalars(select(User).where(User.institution_id == u.institution_id, User.chat_agent == True,  # noqa: E712
                                        User.active == True)).all()  # noqa: E712
    return [{'id': a.id, 'name': name_of(a), 'online': a.id in online} for a in rows if a.role in STAFF]


@router.get('/analytics')
def analytics(start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'),
              u: User = Depends(require_roles(*STAFF)), s: Session = Depends(db)):
    """Chat volumes, waiting times, missed chats, ratings and each consultant's load."""
    sweep_missed(s, u.institution_id)
    q = select(ChatSession).where(ChatSession.institution_id == u.institution_id)
    try:
        if start:
            q = q.where(ChatSession.created_at >= datetime.strptime(start, '%Y-%m-%d'))
        if end:
            q = q.where(ChatSession.created_at <= datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1))
    except ValueError:
        raise HTTPException(422, 'Dates must look like 2026-11-30.')
    rows = s.scalars(q).all()
    secs = lambda a, b: (b - a).total_seconds() if a and b else None  # noqa: E731
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    waits = [w for w in (secs(r.created_at, r.accepted_at) for r in rows) if w is not None]
    durations = [d for d in (secs(r.accepted_at, r.closed_at) for r in rows if r.status == 'closed') if d is not None]
    ratings = [r.rating for r in rows if r.rating]
    msg_counts = Counter(sid for (sid,) in s.execute(select(ChatMessage.session_id).where(
        ChatMessage.session_id.in_([r.id for r in rows]), ChatMessage.sender != 'system')).all()) if rows else Counter()
    per_agent = defaultdict(lambda: {'chats': 0, 'waits': [], 'ratings': []})
    for r in rows:
        if r.agent_id:
            p = per_agent[r.agent_id]
            p['chats'] += 1
            if r.accepted_at:
                p['waits'].append(secs(r.created_at, r.accepted_at))
            if r.rating:
                p['ratings'].append(r.rating)
    names = {x.id: name_of(x) for x in s.scalars(select(User).where(User.id.in_(list(per_agent) or [0]))).all()}
    by_hour = Counter(local_now(r.created_at).hour for r in rows)
    by_day = Counter(local_now(r.created_at).strftime('%Y-%m-%d') for r in rows)
    answered = [r for r in rows if r.accepted_at]
    return {
        'chats': len(rows), 'answered': len(answered), 'missed': sum(1 for r in rows if r.status == 'missed'),
        'open_now': sum(1 for r in rows if r.status in OPEN_STATES),
        'answer_rate': round(len(answered) / len([r for r in rows if r.status != 'waiting']) * 100, 1)
        if [r for r in rows if r.status != 'waiting'] else None,
        'average_wait_seconds': avg(waits), 'average_duration_minutes': avg([d / 60 for d in durations]),
        'average_messages': avg(list(msg_counts.values())), 'average_rating': avg(ratings), 'ratings': len(ratings),
        'by_agent': sorted([{'agent': names.get(k, '?'), 'chats': v['chats'], 'average_wait_seconds': avg(v['waits']),
                             'average_rating': avg(v['ratings'])} for k, v in per_agent.items()], key=lambda x: -x['chats']),
        'by_hour': [{'label': f'{h:02d}:00', 'count': by_hour[h]} for h in sorted(by_hour)],
        'by_day': [{'label': d, 'count': by_day[d]} for d in sorted(by_day)],
    }


