
import hashlib
import os

import redis
from fastapi import HTTPException, Request
from redis.exceptions import RedisError


def get_redis_client():
    """Create a Redis client using the configured environment URL."""
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        raise RedisError("REDIS_URL is not configured")

    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def enforce_rate_limit(
    request: Request,
    endpoint: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Limit requests using an atomic Redis counter."""
    trust_render_proxy = (
        os.getenv("TRUST_RENDER_PROXY", "").lower() == "true"
    )

    if trust_render_proxy:
        client_ip = request.headers.get("cf-connecting-ip", "").strip()

        if not client_ip:
            raise HTTPException(
                status_code=503,
                detail="Client IP verification is temporarily unavailable.",
            )
    else:
        client_ip = request.client.host if request.client else "unknown"

    # Hash the IP so the raw address is not stored in Redis keys.
    identifier = hashlib.sha256(client_ip.encode()).hexdigest()
    redis_key = f"geofield:rate-limit:{endpoint}:{identifier}"

    # Atomically increment the counter and set its expiry on first use.
    script = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    local ttl = redis.call('TTL', KEYS[1])
    return {count, ttl}
    """

    try:
        client = get_redis_client()
        result = client.eval(script, 1, redis_key, window_seconds)
        request_count = int(result[0])
        remaining_seconds = max(int(result[1]), 1)

    except RedisError:
        # Fail closed: do not silently disable protection if Redis fails.
        raise HTTPException(
            status_code=503,
            detail="Request protection is temporarily unavailable. Please try again later.",
        )

    if request_count > limit:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(remaining_seconds)},
        )
