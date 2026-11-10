import json
import logging
import re
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import RelayDatum, SignalBlob

log = logging.getLogger(__name__)

SIGNAL_TTL = timedelta(seconds=120)
SIGNAL_MAX_PAYLOAD = 16384
SIGNAL_MAX_QUEUE = 200
SIGNAL_POLL_LIMIT = 64

RELAY_TTL = timedelta(seconds=30)
RELAY_MAX_CHUNK = 8192
RELAY_MAX_BATCH = 64
RELAY_FETCH_LIMIT = 128
RELAY_MAX_QUEUE = 4000

HANDLE_RE = re.compile(r"age1[0-9a-z]{20,120}")
SESSION_RE = re.compile(r"[0-9a-f]{8,64}")
B64_RE = re.compile(r"[A-Za-z0-9+/=]+")


def ok(**extra):
    return JsonResponse({"ok": True, **extra})


def fail(status, msg):
    return JsonResponse({"ok": False, "error": msg}, status=status)


def _text(data, key, strip=True):
    value = data.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip() if strip else value


def _reap(now):
    """Drop expired rows. Non-fatal, matching the PHP version."""
    for model in (SignalBlob, RelayDatum):
        try:
            model.objects.filter(expires_at__lt=now).delete()
        except DatabaseError:
            log.exception("reap failed for %s", model.__name__)


def _take(queryset, limit):
    """Read up to `limit` rows oldest-first and delete them in one transaction."""
    with transaction.atomic():
        rows = list(queryset.select_for_update().order_by("id")[:limit])
        if rows:
            queryset.model.objects.filter(pk__in=[r.pk for r in rows]).delete()
    return rows


def _send(data, now):
    to = _text(data, "to")
    payload = _text(data, "payload", strip=False)
    if not HANDLE_RE.fullmatch(to):
        return fail(422, "bad_recipient")
    if not payload or len(payload) > SIGNAL_MAX_PAYLOAD or not B64_RE.fullmatch(payload):
        return fail(422, "bad_payload")

    with transaction.atomic():
        if SignalBlob.objects.filter(recipient=to).count() >= SIGNAL_MAX_QUEUE:
            return fail(429, "queue_full")
        SignalBlob.objects.create(recipient=to, payload=payload, expires_at=now + SIGNAL_TTL)
    return ok()


def _poll(data, now):
    handle = _text(data, "for")
    if not HANDLE_RE.fullmatch(handle):
        return fail(422, "bad_recipient")

    qs = SignalBlob.objects.filter(recipient=handle, expires_at__gte=now)
    rows = _take(qs, SIGNAL_POLL_LIMIT)
    return ok(blobs=[r.payload for r in rows])


def _data(data, now):
    to = _text(data, "to")
    session = _text(data, "session").lower()
    chunks = data.get("chunks")
    if not HANDLE_RE.fullmatch(to):
        return fail(422, "bad_recipient")
    if not SESSION_RE.fullmatch(session):
        return fail(422, "bad_session")
    if not isinstance(chunks, list) or not 0 < len(chunks) <= RELAY_MAX_BATCH:
        return fail(422, "bad_batch")
    for c in chunks:
        if not isinstance(c, str) or not c or len(c) > RELAY_MAX_CHUNK or not B64_RE.fullmatch(c):
            return fail(422, "bad_chunk")

    with transaction.atomic():
        pending = RelayDatum.objects.filter(recipient=to, session=session).count()
        if pending >= RELAY_MAX_QUEUE:
            return fail(429, "queue_full")
        RelayDatum.objects.bulk_create([
            RelayDatum(recipient=to, session=session, payload=c, expires_at=now + RELAY_TTL)
            for c in chunks
        ])
    return ok(stored=len(chunks))


def _fetch(data, now):
    handle = _text(data, "for")
    session = _text(data, "session").lower()
    if not HANDLE_RE.fullmatch(handle):
        return fail(422, "bad_recipient")
    if not SESSION_RE.fullmatch(session):
        return fail(422, "bad_session")

    qs = RelayDatum.objects.filter(recipient=handle, session=session, expires_at__gte=now)
    rows = _take(qs, RELAY_FETCH_LIMIT)
    return ok(chunks=[r.payload for r in rows])


HANDLERS = {"send": _send, "poll": _poll, "data": _data, "fetch": _fetch}


@csrf_exempt
@require_POST
def signal(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return fail(400, "bad_json")
    if not isinstance(data, dict):
        return fail(400, "bad_json")

    now = timezone.now()
    _reap(now)

    op = _text(data, "op").lower()
    handler = HANDLERS.get(op)
    if handler is None:
        return fail(400, "bad_op")

    try:
        return handler(data, now)
    except DatabaseError:
        log.exception("signal op %r failed", op)
        return fail(500, "db_error")

