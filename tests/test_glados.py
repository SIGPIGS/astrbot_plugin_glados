"""Tests for astrbot_plugin_glados pure logic and check-in flow."""

import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as glados  # noqa: E402

GLD_COOKIE = "koa:sess=abc; koa:sess.sig=xyz"
BOTH_COOKIE = "gld:sess=a; gld:sess.sig=b; koa:sess=c; koa:sess.sig=d"


class FakeResponse:
    """Minimal aiohttp response stub."""

    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._text = text if payload is None else json.dumps(payload)

    async def text(self):
        return self._text


class FakeSession:
    """aiohttp session stub keyed by URL with request capture."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome


def account(**overrides):
    fields = {
        "name": "test",
        "cookie": GLD_COOKIE,
        "user_agent": "",
        "notify_umo": "",
        "sites": tuple(glados.complete_cookie_sites(GLD_COOKIE)),
    }
    fields.update(overrides)
    return glados.GladosAccount(**fields)


def ok_checkin_routes(domain, code=0, points="5", left_days="23", total="1230"):
    return {
        f"https://{domain}/api/user/checkin": FakeResponse(
            payload={"code": code, "points": points, "message": "Checkin OK"}
        ),
        f"https://{domain}/api/user/status": FakeResponse(
            payload={"code": 0, "data": {"leftDays": left_days}}
        ),
        f"https://{domain}/api/user/points": FakeResponse(
            payload={"code": 0, "points": total}
        ),
    }


def test_complete_cookie_sites():
    assert glados.complete_cookie_sites(GLD_COOKIE) == ["railgun.info"]
    assert glados.complete_cookie_sites("gld:sess=a; gld:sess.sig=b") == [
        "glados.cloud"
    ]
    assert glados.complete_cookie_sites(BOTH_COOKIE) == list(glados.DOMAINS)
    assert glados.complete_cookie_sites("foo=bar") == []
    assert glados.complete_cookie_sites("gld:sess=a") == []


def test_next_run_delay():
    now = datetime(2026, 9, 28, 8, 0, 0)
    assert glados.next_run_delay(now, 9) == 3600.0
    assert glados.next_run_delay(now, 7) == 23 * 3600.0
    assert glados.next_run_delay(now, 8) == 24 * 3600.0
    later = now + timedelta(seconds=1)
    assert glados.next_run_delay(later, 8) == 24 * 3600.0 - 1


def test_load_accounts_validates_and_defaults():
    accounts = glados.load_accounts(
        [
            {
                "name": "main",
                "cookie": GLD_COOKIE,
                "user_agent": "MyAccountUA",
                "notify_umo": "napcat:GroupMessage:1",
            },
            {"cookie": BOTH_COOKIE},
        ]
    )
    assert [a.name for a in accounts] == ["main", "账户2"]
    assert accounts[0].sites == ("railgun.info",)
    assert accounts[0].user_agent == "MyAccountUA"
    assert accounts[0].notify_umo == "napcat:GroupMessage:1"
    assert accounts[1].user_agent == ""
    assert accounts[1].notify_umo == ""
    assert accounts[1].sites == glados.DOMAINS

    with pytest.raises(glados.GladosConfigError):
        glados.load_accounts([{"name": "no-cookie"}])
    with pytest.raises(glados.GladosConfigError):
        glados.load_accounts(["not-a-dict"])
    with pytest.raises(glados.GladosConfigError):
        glados.load_accounts("nope")


def test_format_result():
    ok = glados.AccountResult("main", ok=True, repeat=False, detail="站点 x · 剩 2 天")
    assert glados.format_result(ok).startswith("✅ GLaDOS 签到成功 [main]")
    repeat = glados.AccountResult("main", ok=True, repeat=True, detail="站点 x")
    assert glados.format_result(repeat).startswith("☑️ GLaDOS 今日已签到 [main]")
    fail = glados.AccountResult("main", ok=False, repeat=False, detail="boom")
    assert glados.format_result(fail).startswith("❌ GLaDOS 签到失败 [main]")


def test_describe_site_error():
    assert "反自动化" in glados.describe_site_error(4, "Automated check-in detected")
    assert "反自动化" in glados.describe_site_error(-2, "Automated check-in detected")
    assert "Cookie" in glados.describe_site_error(-2, "没有权限")
    assert "wtf" in glados.describe_site_error(9, "wtf")


@pytest.mark.asyncio
async def test_run_account_checkin_success():
    session = FakeSession(ok_checkin_routes("railgun.info"))
    result = await glados.run_account_checkin(session, account(), 5)
    assert result.ok and not result.repeat
    assert "railgun.info" in result.detail
    assert "获得 5 积分" in result.detail
    assert "剩余 23 天" in result.detail
    assert "总积分 1230" in result.detail


@pytest.mark.asyncio
async def test_run_account_checkin_repeat():
    session = FakeSession(ok_checkin_routes("railgun.info", code=1))
    result = await glados.run_account_checkin(session, account(), 5)
    assert result.ok and result.repeat


@pytest.mark.asyncio
async def test_run_account_checkin_falls_back_to_second_domain():
    routes = ok_checkin_routes("railgun.info")
    routes["https://glados.cloud/api/user/checkin"] = FakeResponse(
        payload={"code": -2, "message": "没有权限"}
    )
    session = FakeSession(routes)
    result = await glados.run_account_checkin(
        session, account(cookie=BOTH_COOKIE, sites=glados.DOMAINS), 5
    )
    assert result.ok
    # Only the second domain's status/points should have been queried.
    queried = [url for _, url, _ in session.requests]
    assert "https://railgun.info/api/user/status" in queried
    assert "https://glados.cloud/api/user/status" not in queried


@pytest.mark.asyncio
async def test_run_account_checkin_all_sites_fail():
    routes = {}
    for domain in glados.DOMAINS:
        routes[f"https://{domain}/api/user/checkin"] = FakeResponse(
            payload={"code": 4, "message": "Automated check-in detected"}
        )
    session = FakeSession(routes)
    result = await glados.run_account_checkin(
        session, account(cookie=BOTH_COOKIE, sites=glados.DOMAINS), 5
    )
    assert not result.ok
    assert result.detail.count("反自动化") == 2


@pytest.mark.asyncio
async def test_run_account_checkin_network_error_recorded():
    routes = {"https://railgun.info/api/user/checkin": aiohttp.ClientError("boom")}
    session = FakeSession(routes)
    result = await glados.run_account_checkin(session, account(), 5)
    assert not result.ok
    assert "网络请求失败" in result.detail


@pytest.mark.asyncio
async def test_run_account_checkin_incomplete_cookie():
    result = await glados.run_account_checkin(
        FakeSession({}), account(sites=()), 5
    )
    assert not result.ok
    assert "会话字段" in result.detail


@pytest.mark.asyncio
async def test_request_mirrors_browser_format():
    session = FakeSession(ok_checkin_routes("railgun.info"))
    await glados.run_account_checkin(session, account(user_agent="MyUA"), 7)
    method, url, kwargs = session.requests[0]
    assert method == "POST" and url.endswith("/api/user/checkin")
    headers = kwargs["headers"]
    assert headers["origin"] == "https://railgun.info"
    assert headers["user-agent"] == "MyUA"
    assert headers["content-type"] == "application/json;charset=UTF-8"
    assert kwargs["data"] == b'{"token":"railgun.info"}'
    assert "referer" not in headers


@pytest.mark.asyncio
async def test_account_user_agent_used_or_default():
    session = FakeSession(ok_checkin_routes("railgun.info"))
    await glados.run_account_checkin(
        session, account(user_agent="LoginBrowserUA"), 7
    )
    used = {kwargs["headers"]["user-agent"] for _, _, kwargs in session.requests}
    assert used == {"LoginBrowserUA"}

    session = FakeSession(ok_checkin_routes("railgun.info"))
    await glados.run_account_checkin(session, account(), 7)
    used = {kwargs["headers"]["user-agent"] for _, _, kwargs in session.requests}
    assert used == {glados.DEFAULT_USER_AGENT}


def test_format_traffic():
    assert glados.format_traffic(512) == "512 B"
    assert glados.format_traffic(1536) == "1.5 KiB"
    assert glados.format_traffic(278776393508) == "259.6 GiB"
    assert glados.format_traffic(3 * 1024**4) == "3.0 TiB"


REAL_STATUS_PAYLOAD = {
    "code": 0,
    "data": {
        "traffic": 278776393508,
        "vip": 31,
        "region": "us",
        "port": 183203,
        "leftDays": "236.0000000000000000",
    },
}
REAL_POINTS_PAYLOAD = {"code": 0, "points": "47.0000000000000000"}


def status_routes():
    return {
        "https://railgun.info/api/user/status": FakeResponse(
            payload=REAL_STATUS_PAYLOAD
        ),
        "https://railgun.info/api/user/points": FakeResponse(
            payload=REAL_POINTS_PAYLOAD
        ),
    }


@pytest.mark.asyncio
async def test_query_account_status_details():
    session = FakeSession(status_routes())
    result = await glados.query_account_status(session, account(), 5)
    assert result.ok
    assert "railgun.info" in result.detail
    assert "剩余 236 天" in result.detail
    assert "已用流量 259.6 GiB" in result.detail
    assert "VIP 31" in result.detail
    assert "区域 us" in result.detail
    assert "端口 183203" in result.detail
    assert "积分 47" in result.detail


@pytest.mark.asyncio
async def test_query_account_status_tolerates_missing_fields():
    routes = status_routes()
    routes["https://railgun.info/api/user/status"] = FakeResponse(
        payload={"code": 0, "data": {"leftDays": "3.5"}}
    )
    routes["https://railgun.info/api/user/points"] = aiohttp.ClientError("boom")
    result = await glados.query_account_status(FakeSession(routes), account(), 5)
    assert result.ok
    assert "剩余 3 天" in result.detail
    assert "已用流量" not in result.detail
    assert "VIP" not in result.detail
    assert "积分" not in result.detail


@pytest.mark.asyncio
async def test_query_account_status_falls_back_to_second_domain():
    routes = {
        "https://glados.cloud/api/user/status": FakeResponse(
            payload={"code": -2, "message": "没有权限"}
        ),
        "https://railgun.info/api/user/status": FakeResponse(
            payload=REAL_STATUS_PAYLOAD
        ),
        "https://railgun.info/api/user/points": FakeResponse(
            payload=REAL_POINTS_PAYLOAD
        ),
    }
    session = FakeSession(routes)
    result = await glados.query_account_status(
        session, account(cookie=BOTH_COOKIE, sites=glados.DOMAINS), 5
    )
    assert result.ok
    assert "剩余 236 天" in result.detail


@pytest.mark.asyncio
async def test_query_account_status_all_fail():
    routes = {
        f"https://{domain}/api/user/status": FakeResponse(
            payload={"code": -2, "message": "没有权限"}
        )
        for domain in glados.DOMAINS
    }
    result = await glados.query_account_status(
        FakeSession(routes), account(cookie=BOTH_COOKIE, sites=glados.DOMAINS), 5
    )
    assert not result.ok
    assert result.detail.count("Cookie 无效") == 2


@pytest.mark.asyncio
async def test_query_account_status_incomplete_cookie():
    result = await glados.query_account_status(FakeSession({}), account(sites=()), 5)
    assert not result.ok
    assert "会话字段" in result.detail
