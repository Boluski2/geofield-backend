from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError
from starlette.requests import Request

from rate_limiter import enforce_rate_limit


def make_request(client_ip="203.0.113.10"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": (client_ip, 12345),
        "scheme": "http",
    }
    return Request(scope)


@patch("rate_limiter.get_redis_client")
def test_request_within_rate_limit_is_allowed(mock_get_redis):
    mock_redis = MagicMock()
    mock_redis.eval.return_value = [1, 900]
    mock_get_redis.return_value = mock_redis

    enforce_rate_limit(
        request=make_request(),
        endpoint="login",
        limit=10,
        window_seconds=900,
    )

    mock_redis.eval.assert_called_once()


@patch("rate_limiter.get_redis_client")
def test_request_over_rate_limit_returns_429(mock_get_redis):
    mock_redis = MagicMock()
    mock_redis.eval.return_value = [11, 600]
    mock_get_redis.return_value = mock_redis

    with pytest.raises(HTTPException) as exc_info:
        enforce_rate_limit(
            request=make_request(),
            endpoint="login",
            limit=10,
            window_seconds=900,
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "600"


@patch("rate_limiter.get_redis_client")
def test_redis_failure_returns_503(mock_get_redis):
    mock_redis = MagicMock()
    mock_redis.eval.side_effect = RedisError("Redis unavailable")
    mock_get_redis.return_value = mock_redis

    with pytest.raises(HTTPException) as exc_info:
        enforce_rate_limit(
            request=make_request(),
            endpoint="login",
            limit=10,
            window_seconds=900,
        )

    assert exc_info.value.status_code == 503

@patch("rate_limiter.get_redis_client")
def test_local_mode_ignores_forged_proxy_header(
    mock_get_redis, monkeypatch
):
    monkeypatch.delenv("TRUST_RENDER_PROXY", raising=False)

    mock_redis = MagicMock()
    mock_redis.eval.return_value = [1, 900]
    mock_get_redis.return_value = mock_redis

    request = make_request()
    request.scope["headers"] = [
        (b"cf-connecting-ip", b"198.51.100.99")
    ]

    enforce_rate_limit(
        request=request,
        endpoint="login",
        limit=10,
        window_seconds=900,
    )

    redis_key = mock_redis.eval.call_args.args[2]
    assert "203.0.113.10" not in redis_key
    assert "198.51.100.99" not in redis_key
    expected_identifier = __import__("hashlib").sha256(
        b"203.0.113.10"
    ).hexdigest()
    assert redis_key.endswith(expected_identifier)


@patch("rate_limiter.get_redis_client")
def test_trusted_proxy_mode_uses_cf_connecting_ip(
    mock_get_redis, monkeypatch
):
    monkeypatch.setenv("TRUST_RENDER_PROXY", "true")

    mock_redis = MagicMock()
    mock_redis.eval.return_value = [1, 900]
    mock_get_redis.return_value = mock_redis

    request = make_request()
    request.scope["headers"] = [
        (b"cf-connecting-ip", b"198.51.100.99")
    ]

    enforce_rate_limit(
        request=request,
        endpoint="login",
        limit=10,
        window_seconds=900,
    )

    redis_key = mock_redis.eval.call_args.args[2]
    expected_identifier = __import__("hashlib").sha256(
        b"198.51.100.99"
    ).hexdigest()
    assert redis_key.endswith(expected_identifier)


def test_trusted_proxy_mode_without_header_returns_503(monkeypatch):
    monkeypatch.setenv("TRUST_RENDER_PROXY", "true")

    with patch("rate_limiter.get_redis_client") as mock_get_redis:
        with pytest.raises(HTTPException) as exc_info:
            enforce_rate_limit(
                request=make_request(),
                endpoint="login",
                limit=10,
                window_seconds=900,
            )

    assert exc_info.value.status_code == 503
    mock_get_redis.assert_not_called()