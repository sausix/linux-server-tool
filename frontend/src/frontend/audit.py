"""Persistent, at-least-once delivery of security events to backend."""
import json
import logging
import threading
import time
import uuid
from sqlalchemy import select, delete, func, text as sql_text
from common.client import DaemonClient
from common.security_events import clean, LIMITS, validate_event
from frontend.models import AuditOutbox, utcnow

logger = logging.getLogger('frontend.security')
logger.setLevel(logging.INFO)


class SecurityAudit:
    def __init__(self, factory, settings):
        self.factory = factory
        self.client = DaemonClient(settings.daemon_endpoint, token=settings.daemon_token, timeout=min(1, settings.daemon_timeout_seconds))
        self.stop_event = threading.Event()
        self.thread = None
        self.failed = False

    def emit(self, event, request=None, user=None, **fields):
        if request is not None:
            from frontend.login_policy import source_ip
            fields = {'source_ip': source_ip(request), 'user_agent': request.headers.get('user-agent', ''), **fields}
            row = request.scope.get('session_record') or {}
            fields.setdefault('session_id', row.get('public_id', ''))
        if user is not None:
            fields = {'username': user.username, 'user_id': user.id, **fields}
        payload = validate_event({'event_id': uuid.uuid4().hex, 'timestamp': utcnow().isoformat(), 'event': event,
                                  **{key: clean(value, LIMITS[key]) for key, value in fields.items() if key in LIMITS and value is not None}})
        text = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
        logger.info('%s', text)
        with self.factory() as db:
            # Local structured logging remains the fallback if the delivery queue fills.
            if db.bind.dialect.name == 'sqlite':
                db.execute(sql_text('BEGIN IMMEDIATE'))
            count = db.scalar(select(func.count()).select_from(AuditOutbox))
            if count >= 10000:
                oldest = db.scalar(select(AuditOutbox.id).order_by(AuditOutbox.created_at).limit(1))
                db.execute(delete(AuditOutbox).where(AuditOutbox.id == oldest))
                logger.error('Security audit outbox full; oldest event retained only in local security logs')
            db.add(AuditOutbox(id=payload['event_id'], payload=text, created_at=time.time()))
            db.commit()

    def flush(self):
        with self.factory() as db:
            rows = db.scalars(select(AuditOutbox).order_by(AuditOutbox.created_at).limit(20)).all()
        for row in rows:
            if self.stop_event.is_set():
                return
            try:
                result = self.client.call('security.event', {'event': json.loads(row.payload)})
                if not isinstance(result, dict) or result.get('accepted') is not True:
                    raise ValueError('Security audit was not acknowledged')
                with self.factory() as db:
                    db.execute(delete(AuditOutbox).where(AuditOutbox.id == row.id)); db.commit()
                self.failed = False
            except Exception:
                if not self.failed:
                    logger.warning('Security audit delivery unavailable; events remain queued for backend')
                self.failed = True
                return

    def start(self):
        self.stop_event.clear()
        def work():
            while not self.stop_event.wait(1):
                try:
                    self.flush()
                except Exception:
                    logger.exception('Security audit delivery worker failed')
        self.thread = threading.Thread(target=work, daemon=True, name='frontend-security-audit')
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
