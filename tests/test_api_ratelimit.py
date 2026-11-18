import asyncio
import logging

import httpx
import pytest

from repost_bot.logging_setup import RedactFilter
from repost_bot.ratelimit import RateLimiter
from repost_bot.telegram_api import BotApi, NetworkError, RetryAfter, TelegramError

TOKEN = "123:SECRET"


def api_with(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BotApi(TOKEN, client=client)


def test_success_and_payload():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["body"] = req.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    api = api_with(h)
    assert asyncio.run(api.copy_message(-2, -1, 5)) == 9
    assert seen["url"].endswith("/copyMessage")


def test_copy_messages_sorts_ids():
    def h(req):
        import json
        ids = json.loads(req.read())["message_ids"]
        assert ids == [1, 2, 3]
        return httpx.Response(200, json={"ok": True, "result": [{"message_id": 7}]})

    assert asyncio.run(api_with(h).copy_messages(-2, -1, [3, 1, 2])) == [7]


def test_429_parsed():
    def h(req):
        return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 12}})

    with pytest.raises(RetryAfter) as ei:
        asyncio.run(api_with(h).copy_message(-2, -1, 5))
    assert ei.value.retry_after == 12


def test_migrate_parsed():
    def h(req):
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "migrated",
                                         "parameters": {"migrate_to_chat_id": -100555}})

    with pytest.raises(TelegramError) as ei:
        asyncio.run(api_with(h).copy_message(-2, -1, 5))
    assert ei.value.migrate_to_chat_id == -100555


def test_network_errors_classified_and_token_redacted():
    def connect_fail(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    def read_fail(req):
        raise httpx.ReadTimeout(f"timeout {req.url}")

    with pytest.raises(NetworkError) as e1:
        asyncio.run(api_with(connect_fail).copy_message(-2, -1, 5))
    assert e1.value.maybe_delivered is False
    assert "SECRET" not in str(e1.value)

    with pytest.raises(NetworkError) as e2:
        asyncio.run(api_with(read_fail).copy_message(-2, -1, 5))
    assert e2.value.maybe_delivered is True


def test_log_filter_redacts():
    rec = logging.LogRecord("x", logging.INFO, "", 0, "url=%s", (f"https://api/bot{TOKEN}/x",), None)
    RedactFilter([TOKEN]).filter(rec)
    assert TOKEN not in rec.getMessage()


def test_rate_limiter_spacing_and_penalty():
    t = [0.0]
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        t[0] += s

    lim = RateLimiter(20, clock=lambda: t[0], sleep=fake_sleep)  # 3 s por mensagem

    async def go():
        await lim.acquire(1)
        await lim.acquire(3)  # espera 3 s
        await lim.acquire(1)  # espera 9 s (álbum de 3)
        lim.penalize(30)
        await lim.acquire(1)

    asyncio.run(go())
    assert slept[0] == pytest.approx(3)
    assert slept[1] == pytest.approx(9)
    assert slept[2] == pytest.approx(30 + 1)  # retry_after + 1 s de margem
