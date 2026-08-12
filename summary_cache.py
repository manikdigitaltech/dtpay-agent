"""
Redis cache for /summary responses.

Two design decisions here go beyond the literal "key = start_date +
end_date + product_id" spec, both for reasons that matter given this
system's existing security model - worth being explicit about rather
than just implementing silently:

1. The key includes uid, but ONLY when cp_product_id is omitted from
   the request (meaning "everything this uid is allowed to see").
   That scope is different for every non-admin uid - two different
   partners' "everything" are genuinely different data. Without uid
   in the key for that case, one partner's cached response could get
   served to a completely different partner who also omitted
   cp_product_id, which would be a real data leak between customers.
   When an explicit cp_product_id IS given, the data is the same
   regardless of who's asking (ownership was already checked before
   the cache is ever consulted, in api.py), so leaving uid out of the
   key there is both safe and better for the cache hit rate - an
   admin and the partner themselves both checking the same product
   share one cache entry instead of each getting their own.

2. What gets cached is the summary DATA (date_range, overall,
   products, generated_at) - never session_id. Every request, hit or
   miss, gets its own freshly-created chat session (cheap - it's a
   single DB insert, not a Claude call) built from that data, rather
   than reusing whatever session the original cache-populating
   request created. The alternative - caching session_id too and
   handing the same one out to everyone who hits this cache entry -
   would mean MAX_QUESTIONS_PER_SESSION and the 24h session expiry end
   up silently shared across unrelated requests that happen to ask
   about the same range/product, which isn't what either of those
   limits are supposed to do.
"""
import json

import redis

from config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD,
    SUMMARY_CACHE_TTL_SECONDS, SUMMARY_CACHE_TTL_TODAY_SECONDS,
)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD,
            decode_responses=True, socket_connect_timeout=5, socket_timeout=5,
        )
    return _client


def build_key(start_date, end_date, cp_product_id, uid):
    if cp_product_id is not None:
        return f"summary:{start_date.isoformat()}:{end_date.isoformat()}:product:{cp_product_id}"
    return f"summary:{start_date.isoformat()}:{end_date.isoformat()}:uid:{uid}"


def determine_ttl(start_date, end_date, today):
    """today is passed in (not computed here) so callers control what
    "now" means for testing, rather than this function reaching for
    the real clock itself."""
    if start_date <= today <= end_date:
        return SUMMARY_CACHE_TTL_TODAY_SECONDS
    return SUMMARY_CACHE_TTL_SECONDS


def get(key):
    """Returns the cached dict, or None on a miss OR on any Redis
    error - a cache being unavailable should degrade to "just compute
    it fresh", never take the whole endpoint down with it."""
    try:
        raw = _get_client().get(key)
    except redis.RedisError:
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def set(key, value, ttl_seconds):
    """Best-effort - a failed cache write shouldn't fail the request
    that's already computed a perfectly good response to return."""
    try:
        _get_client().setex(key, ttl_seconds, json.dumps(value))
    except redis.RedisError:
        pass
