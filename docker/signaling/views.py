import contextlib
import hashlib
import json
import logging
import re
import secrets

from datetime import datetime, timedelta
from typing import TypeAlias

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ._settings import MAX_TAKE_CEILING, settings, validated_max_batch
from .models import Nameplate, RateLimit, RelayDatum, SignalBlob


Data: TypeAlias = dict[str, str | list[str]]

B64_RE = re.compile(r'^[A-Za-z0-9+/]+[=]*$')
HANDLE_RE = re.compile(r'age1[0-9a-z]{20,120}')
SESSION_RE = re.compile(r'[0-9a-f]{8,64}')
MBOX_RE = re.compile(r'[A-Za-z0-9_-]{43}')
TOKEN_RE = re.compile(r'[0-9a-f]{64}')

# Per-IP rate limits (4.0). Counted over a sliding window on a salted hash of the client IP.
# data and fetch (the relay-forward stream) are not limited.
RATE_WINDOW_SECONDS = 60
RATE_LIMITS = {
    'poll': 1200, 'mbox.poll': 1200,
    'send': 300, 'mbox.send': 300,
    'nameplate.claim': 20,
}
RELAY_VERSION = '2.0'
RELAY_FEATURES = ['mbox', 'nameplate', 'sealed', 'ratelimit']
NAMEPLATE_TTL_SECONDS = 600
NAMEPLATE_SMALL_MAX = 999
NAMEPLATE_LARGE_MAX = 9999

log = logging.getLogger(__name__)


def _max_body_bytes() -> int:
    max_batch = validated_max_batch()
    max_send = settings.SIGNAL_MAX_PAYLOAD
    max_data = (4 + settings.RELAY_MAX_CHUNK) * max_batch
    overhead = 512
    return overhead + max(max_send, max_data)


def _read_json_body(request) -> dict | JsonResponse:
    max_bytes = _max_body_bytes()

    content_length = request.META.get('CONTENT_LENGTH')
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return fail(413, 'payload_too_large')
        except ValueError:
            return fail(400, 'bad_json')

    try:
        raw = request.read(max_bytes + 1)
    except RequestDataTooBig:
        return fail(413, 'payload_too_large')

    if max_bytes < len(raw):
        return fail(413, 'payload_too_large')

    try:
        data = json.loads(raw or b'{}')
    except (ValueError, UnicodeDecodeError):
        return fail(400, 'bad_json')
    else:
        if not isinstance(data, dict):
            return fail(400, 'bad_json')
        return data


def _expired(qs, now: datetime):
    return qs.filter(expires_at__lt=now)


def _unexpired(qs, now: datetime):
    return qs.filter(expires_at__gte=now)


def _queue_full(qs, /, maximum: int, incoming: int = 1) -> bool:
    return maximum < (incoming + qs.count())


def ok(**extra) -> JsonResponse:
    return JsonResponse({'ok': True, **extra})


def fail(status: int, msg: str) -> JsonResponse:
    return JsonResponse({'ok': False, 'error': msg}, status=status)


def _text(data: Data, key: str, strip: bool = True) -> str:
    if not isinstance(data, dict):
        return ''
    value = data.get(key)
    if not isinstance(value, str):
        return ''
    return value.strip() if strip else value


def _int(data: Data, key: str) -> int:
    try:
        return int(data.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _valid_payload(payload: str) -> bool:
    return 0 < len(payload) <= settings.SIGNAL_MAX_PAYLOAD and bool(B64_RE.fullmatch(payload))


def _reap(now: datetime) -> None:
    """Drop expired rows. Non-fatal, matching the PHP version."""
    for model in (SignalBlob, RelayDatum, Nameplate):
        try:
            _expired(model.objects.all(), now).delete()
        except DatabaseError:
            log.exception('reap failed for %s', model.__name__)
    try:
        RateLimit.objects.filter(at__lt=now - timedelta(seconds=RATE_WINDOW_SECONDS)).delete()
    except DatabaseError:
        log.exception('reap failed for RateLimit')


def _take(queryset, limit: int) -> list:
    """Read up to `limit` rows oldest-first and delete them in one transaction."""
    if not (0 < limit <= MAX_TAKE_CEILING):
        raise ValueError('"limit" was not above zero or was over nine hundred')
    with transaction.atomic(durable=True):
        rows = list(queryset.select_for_update().order_by('id')[:limit])
        if rows:
            queryset.model.objects.filter(pk__in={r.pk for r in rows}).delete()
    return rows


def _client_bucket(request) -> str:
    """Salted SHA-256 of the client IP. The IP itself is never stored."""
    ip = request.META.get('REMOTE_ADDR', '')
    return hashlib.sha256(f'{settings.RATE_SALT}|{ip}'.encode()).hexdigest()


def _rate_limited(request, op: str, now: datetime) -> bool:
    """Count this request against (client, op). True means reject with 429.

    Fails open on database errors, including a missing table on a relay that
    hasn't run the 4.0 schema yet. A broken limiter must not take signaling down.
    """
    limit = RATE_LIMITS.get(op, 0)
    if limit <= 0:
        return False
    bucket = _client_bucket(request)
    try:
        recent = RateLimit.objects.filter(
            bucket=bucket, op=op, at__gte=now - timedelta(seconds=RATE_WINDOW_SECONDS),
        )
        if recent.count() >= limit:
            return True
        RateLimit.objects.create(bucket=bucket, op=op, at=now)
    except DatabaseError:
        log.exception('rate limit check failed for %r', op)
    return False


def _store_blob(to: str, payload: str, now: datetime) -> JsonResponse:
    """Queue one blob for a handle or mailbox id. The caller has validated both."""
    signal_expires = now + timedelta(seconds=settings.SIGNAL_TTL_SECONDS)
    with transaction.atomic(durable=True):
        qs = _unexpired(SignalBlob.objects.filter(recipient=to), now)
        if _queue_full(qs, maximum=settings.SIGNAL_MAX_QUEUE):
            return fail(429, 'queue_full')
        SignalBlob.objects.create(recipient=to, payload=payload, expires_at=signal_expires)
    return ok()


def _take_blobs(recipient: str, now: datetime) -> JsonResponse:
    qs = _unexpired(SignalBlob.objects.filter(recipient=recipient), now)
    rows = _take(qs, settings.SIGNAL_POLL_LIMIT)
    return ok(blobs=[r.payload for r in rows])


def _send(data: Data, now: datetime) -> JsonResponse:
    to = _text(data, 'to')
    if not HANDLE_RE.fullmatch(to):
        return fail(422, 'bad_recipient')

    payload = _text(data, 'payload', strip=False)
    if not _valid_payload(payload):
        return fail(422, 'bad_payload')
    return _store_blob(to, payload, now)


def _poll(data: Data, now: datetime) -> JsonResponse:
    handle = _text(data, 'for')
    if not HANDLE_RE.fullmatch(handle):
        return fail(422, 'bad_recipient')
    return _take_blobs(handle, now)


def _data(data: Data, now: datetime) -> JsonResponse:
    to = _text(data, 'to')
    if not HANDLE_RE.fullmatch(to):
        return fail(422, 'bad_recipient')

    session = _text(data, 'session').lower()
    if not SESSION_RE.fullmatch(session):
        return fail(422, 'bad_session')

    chunks = data.get('chunks')
    if not (isinstance(chunks, list) and (0 < (chunks_length := len(chunks)) <= validated_max_batch())):
        return fail(422, 'bad_batch')
    for chunk in chunks:
        accept_chunk = (
            isinstance(chunk, str)
            and 0 < len(chunk) <= settings.RELAY_MAX_CHUNK
            and B64_RE.fullmatch(chunk)
        )
        if not accept_chunk:
            return fail(422, 'bad_chunk')

    relay_expires = now + timedelta(seconds=settings.RELAY_TTL_SECONDS)
    with transaction.atomic(durable=True):
        qs = _unexpired(RelayDatum.objects.filter(recipient=to, session=session), now)
        if _queue_full(qs, incoming=chunks_length, maximum=settings.RELAY_MAX_QUEUE):
            return fail(429, 'queue_full')
        RelayDatum.objects.bulk_create(
            RelayDatum(recipient=to, session=session, payload=chunk, expires_at=relay_expires)
            for chunk in chunks
        )
    return ok(stored=chunks_length)


def _fetch(data: Data, now: datetime) -> JsonResponse:
    handle = _text(data, 'for')
    if not HANDLE_RE.fullmatch(handle):
        return fail(422, 'bad_recipient')

    session = _text(data, 'session').lower()
    if not SESSION_RE.fullmatch(session):
        return fail(422, 'bad_session')

    qs = _unexpired(RelayDatum.objects.filter(recipient=handle, session=session), now)
    rows = _take(qs, settings.RELAY_FETCH_LIMIT)
    return ok(chunks=[r.payload for r in rows])


# ---- relay 2.0: capability detection, private mailboxes, nameplates ----

def _info(data: Data, now: datetime) -> JsonResponse:
    return ok(version=RELAY_VERSION, features=RELAY_FEATURES)


def _mbox_send(data: Data, now: datetime) -> JsonResponse:
    to = _text(data, 'to')
    if not MBOX_RE.fullmatch(to):
        return fail(422, 'bad_mailbox')
    payload = _text(data, 'payload', strip=False)
    if not _valid_payload(payload):
        return fail(422, 'bad_payload')
    return _store_blob(to, payload, now)


def _mbox_poll(data: Data, now: datetime) -> JsonResponse:
    for_ = _text(data, 'for')
    if not MBOX_RE.fullmatch(for_):
        return fail(422, 'bad_mailbox')
    return _take_blobs(for_, now)


def _nameplate_claim(data: Data, now: datetime) -> JsonResponse:
    active = Nameplate.objects.filter(expires_at__gte=now).count()
    if active >= NAMEPLATE_LARGE_MAX:
        return fail(503, 'nameplates_exhausted')
    # Short numbers while the relay is quiet; widen once half the short range is in use.
    max_number = NAMEPLATE_SMALL_MAX if active < NAMEPLATE_SMALL_MAX // 2 else NAMEPLATE_LARGE_MAX

    token = secrets.token_hex(32)
    expires = now + timedelta(seconds=NAMEPLATE_TTL_SECONDS)
    for _ in range(40):
        number = secrets.randbelow(max_number) + 1
        try:
            # Savepoint, so a duplicate-key failure doesn't poison the outer transaction.
            with transaction.atomic():
                Nameplate.objects.create(nameplate=number, token=token, expires_at=expires)
        except IntegrityError:
            continue  # number already taken by another claim
        return ok(nameplate=number, token=token, ttl=NAMEPLATE_TTL_SECONDS)
    return fail(503, 'nameplates_busy')


def _nameplate_release(data: Data, now: datetime) -> JsonResponse:
    number = _int(data, 'nameplate')
    token = _text(data, 'token').lower()
    if not (1 <= number <= NAMEPLATE_LARGE_MAX and TOKEN_RE.fullmatch(token)):
        return fail(422, 'bad_nameplate')
    # Only the claimer's token releases a nameplate early. Expiry handles the rest.
    Nameplate.objects.filter(nameplate=number, token=token).delete()
    return ok()


HANDLERS = {
    'send': _send,
    'poll': _poll,
    'data': _data,
    'fetch': _fetch,
    'info': _info,
    'mbox.send': _mbox_send,
    'mbox.poll': _mbox_poll,
    'nameplate.claim': _nameplate_claim,
    'nameplate.release': _nameplate_release,
}


@csrf_exempt
@require_POST
def signal(request):
    parsed = _read_json_body(request)
    if isinstance(parsed, JsonResponse):
        return parsed
    data = parsed

    now = timezone.now()
    try:
        op = _text(data, 'op').lower()

        handler = HANDLERS.get(op, None)
        if handler is None:
            return fail(400, 'bad_op')

        if _rate_limited(request, op, now):
            return fail(429, 'rate_limited')

        try:
            return handler(data, now)
        except DatabaseError:
            log.exception('signal op %r failed', op)
            return fail(500, 'db_error')
    finally:
        with contextlib.suppress(Exception):
            _reap(now)

