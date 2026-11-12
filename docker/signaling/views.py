import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from datetime import datetime, timedelta
from typing import TypeAlias

from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ._settings import MAX_TAKE_CEILING, settings, validated_max_batch
from .models import RelayDatum, SignalBlob


Data: TypeAlias = dict[str, int | str | list[str]]

B64_RE = re.compile(r'^[A-Za-z0-9+/]+[=]*$')
HANDLE_RE = re.compile(r'age1[0-9a-z]{20,120}')
MBOX_ID_RE = re.compile(r'[A-Za-z0-9_-]{43}')
SESSION_RE = re.compile(r'[0-9a-f]{8,64}')

VERSION = '2.0'
FEATURES = ['mbox', 'nameplate', 'sealed', 'ratelimit']

NAMEPLATE_SMALL_POOL = 999
NAMEPLATE_FULL_POOL = 9999
NAMEPLATE_TRIES = 20

TIME_BASE_EPOCH = time.time()
TIME_BASE_MONOTONIC = time.monotonic()

log = logging.getLogger(__name__)


def _max_body_bytes() -> int:
    max_batch = validated_max_batch()
    max_send = settings.SIGNAL_MAX_PAYLOAD
    max_data = (4 + settings.RELAY_MAX_CHUNK) * max_batch
    overhead = 512
    return overhead + max(max_send, max_data)


def _read_json_body(request) -> dict | JsonResponse:
    max_bytes = _max_body_bytes()

    content_length = request.META.get('CONTENT_LENGTH', None)
    if content_length is not None:
        content_length_int = 0
        try:
            content_length_int = int(content_length)
        except (TypeError, ValueError):
            pass

        if max_bytes < content_length_int:
            return fail(413, 'payload_too_large')

    try:
        raw = request.read(1 + max_bytes)
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


def _int(data: Data, key: str) -> int:
    if not isinstance(data, dict):
        return 0
    value = data.get(key, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(data: Data, key: str, strip: bool = True) -> str:
    if not isinstance(data, dict):
        return ''
    value = data.get(key)
    if not isinstance(value, str):
        return ''
    return value.strip() if strip else value


def _is_mailbox_id(value: str) -> bool:
    """43 base64url characters that decode to exactly 32 bytes."""
    if not MBOX_ID_RE.fullmatch(value):
        return False
    try:
        return 32 == len(base64.urlsafe_b64decode(value + '='))
    except (binascii.Error, ValueError):
        return False


def _valid_payload(payload: str, decoded: bool = False) -> bytes | None:
    if not B64_RE.fullmatch(payload):
        return
    if not decoded:
        return payload if 0 < len(payload) <= settings.MBOX_MAX_PAYLOAD else None
    try:
        raw = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return
    return raw if 0 < len(raw) <= settings.MBOX_MAX_PAYLOAD else None


def _client_hash(request) -> str:
    salt = getattr(settings, 'RATE_LIMIT_SALT', settings.SECRET_KEY)
    ip = request.META.get('REMOTE_ADDR', '')
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()


def _rate_limited(request, op: str) -> bool:
    limit = _int(settings.RATE_LIMITS, op)
    if 0 == limit:
        return False
    window = max(2, settings.RATE_WINDOW_SECONDS)
    near_time = TIME_BASE_EPOCH + (time.monotonic() - TIME_BASE_MONOTONIC)
    bucket = int(near_time // window)
    key = f'rl:{op}:{bucket}:{_client_hash(request)}'
    cache.add(key, 0, timeout=(1 + window))
    try:
        count = cache.incr(key)
    except ValueError:  # evicted between add and incr; fail open
        return False
    return count > limit


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _reap(now: datetime) -> None:
    """Drop expired rows. Non-fatal, matching the PHP version."""
    for model in (SignalBlob, RelayDatum):
        try:
            _expired(model.objects.all(), now).delete()
        except DatabaseError:
            log.exception('reap failed for %s', model.__name__)


def _take(queryset, limit: int) -> list:
    """Read up to `limit` rows oldest-first and delete them in one transaction."""
    if not (0 < limit <= MAX_TAKE_CEILING):
        raise ValueError('"limit" was not above zero or was over nine hundred')
    with transaction.atomic(durable=True):
        rows = list(queryset.select_for_update().order_by('id')[:limit])
        if rows:
            queryset.model.objects.filter(pk__in={r.pk for r in rows}).delete()
    return rows


def _send(data: Data, now: datetime) -> JsonResponse:
    to = _text(data, 'to')
    if not HANDLE_RE.fullmatch(to):
        return fail(422, 'bad_recipient')

    payload = _text(data, 'payload', strip=False)
    # validate payload by size and base64 alphabet
    if not ((0 < len(payload) <= settings.SIGNAL_MAX_PAYLOAD) and B64_RE.fullmatch(payload)):
        return fail(422, 'bad_payload')

    signal_expires = now + timedelta(seconds=settings.SIGNAL_TTL_SECONDS)
    with transaction.atomic(durable=True):
        qs = _unexpired(SignalBlob.objects.filter(recipient=to), now)
        if _queue_full(qs, maximum=settings.SIGNAL_MAX_QUEUE):
            return fail(429, 'queue_full')
        SignalBlob.objects.create(recipient=to, payload=payload, expires_at=signal_expires)
    return ok()


def _poll(data: Data, now: datetime) -> JsonResponse:
    handle = _text(data, 'for')
    if not HANDLE_RE.fullmatch(handle):
        return fail(422, 'bad_recipient')

    qs = _unexpired(SignalBlob.objects.filter(recipient=handle), now)
    rows = _take(qs, settings.SIGNAL_POLL_LIMIT)
    return ok(blobs=[r.payload for r in rows])


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


def _info(data: Data, now: datetime) -> JsonResponse:
    return ok(version=VERSION, features=FEATURES)


def _mbox_send(data: Data, now: datetime) -> JsonResponse:
    to = _text(data, 'to')
    if not _is_mailbox_id(to):
        return fail(422, 'bad_recipient')

    payload = _text(data, 'payload', strip=False)
    if _valid_payload(payload) is None:
        return fail(422, 'bad_payload')

    signal_expires = now + timedelta(seconds=settings.SIGNAL_TTL_SECONDS)
    with transaction.atomic(durable=True):
        qs = _unexpired(SignalBlob.objects.filter(recipient=to), now)
        if _queue_full(qs, maximum=settings.SIGNAL_MAX_QUEUE):
            return fail(429, 'queue_full')
        SignalBlob.objects.create(recipient=to, payload=payload, expires_at=signal_expires)
    return ok()


def _mbox_poll(data: Data, now: datetime) -> JsonResponse:
    handle = _text(data, 'for')
    if not _is_mailbox_id(handle):
        return fail(422, 'bad_recipient')

    qs = _unexpired(SignalBlob.objects.filter(recipient=handle), now)
    rows = _take(qs, settings.SIGNAL_POLL_LIMIT)
    return ok(blobs=[r.payload for r in rows])


def _nameplate_claim(data: Data, now: datetime) -> JsonResponse:
    in_use = RelayDatum.objects.filter(
        recipient__gte='1',
        recipient__lte='9999',
        payload='nameplate',
        expires_at__gte=now,
    ).count()
    # 1..999 while fewer than half of that pool is in use, then 1..9999
    pool_max = NAMEPLATE_SMALL_POOL if in_use * 2 < NAMEPLATE_SMALL_POOL else NAMEPLATE_FULL_POOL
    token = secrets.token_urlsafe(24)

    nameplate_expires = now + timedelta(seconds=settings.NAMEPLATE_TTL_SECONDS)
    for _ in range(NAMEPLATE_TRIES):
        number = 1 + secrets.randbelow(pool_max)
        # Check every row, not just live ones: an expired row still holds the
        # unique slot until _reap deletes it.
        if RelayDatum.objects.filter(recipient=str(number)).exists():
            continue
        try:
            with transaction.atomic(durable=True):
                # Switched to get_or_create because of how RelayDatum is defined
                # The original used create and depended on a unique constraint.
                _, created = RelayDatum.objects.get_or_create(
                    recipient=str(number),
                    defaults=dict(
                        session=_token_hash(token),
                        payload='nameplate',
                        expires_at=nameplate_expires,
                    ),
                )
                if not created:
                    continue
        except IntegrityError:  # lost a race for this number; try another
            continue
        return ok(nameplate=number, token=token, ttl=int(NAMEPLATE_TTL.total_seconds()))

    return fail(503, 'nameplates_exhausted')


def _nameplate_release(data: Data, now: datetime) -> JsonResponse:
    number = data.get('nameplate')
    if type(number) is not int or not 1 <= number <= NAMEPLATE_FULL_POOL:
        return fail(422, 'bad_nameplate')

    token = _text(data, 'token', strip=False)
    if not token:
        return fail(422, 'bad_token')

    row = RelayDatum.objects.filter(recipient=str(number)).first()
    if row is None:  # already gone; releasing is idempotent
        return ok()
    if not hmac.compare_digest(row.session, _token_hash(token)):
        return fail(403, 'bad_token')
    row.delete()
    return ok()


HANDLERS = {
    'info': _info,
    'send': _send,
    'poll': _poll,
    'data': _data,
    'fetch': _fetch,
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
        if _rate_limited(request, op):
            return fail(429, 'rate_limited')

        try:
            return handler(data, now)
        except DatabaseError:
            log.exception('signal op %r failed', op)
            return fail(500, 'db_error')
    finally:
        with contextlib.suppress(Exception):
            _reap(now)

