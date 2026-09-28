"""AstrBot plugin: scheduled GLaDOS check-in with per-account UMO notifications.

Performs the daily GLaDOS (glados.cloud / railgun.info) check-in for each
configured account at a fixed hour, then pushes the result to the session
(UMO) configured for that account. Also exposes a manual trigger command.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter

DOMAINS = ("glados.cloud", "railgun.info")
SITE_COOKIE_KEYS: dict[str, tuple[str, ...]] = {
    "glados.cloud": ("gld:sess", "gld:sess.sig"),
    "railgun.info": ("koa:sess", "koa:sess.sig"),
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
PERMISSION_ERROR_HINTS = ("没有权限", "no permission")
AUTOMATION_ERROR_HINTS = ("automated check-in detected",)

CODE_SUCCESS = 0
CODE_REPEAT = 1
CODE_FAILURE = -2
CODE_AUTOMATION = 4


class GladosConfigError(ValueError):
    """The plugin configuration is structurally invalid."""


class GladosApiError(Exception):
    """A GLaDOS HTTP request failed (network error or non-JSON body)."""


@dataclass(frozen=True, slots=True)
class GladosAccount:
    """One configured GLaDOS account.

    Attributes:
        name: Display name used in notifications and logs.
        cookie: Raw Cookie header value copied from the browser.
        user_agent: Per-account User-Agent (empty falls back to the built-in
            default, because GLaDOS checks the platform against the browser
            the account was logged in from).
        notify_umo: UMO to receive this account's results (empty disables).
        sites: Domains whose session-cookie keys are complete in ``cookie``.
    """

    name: str
    cookie: str
    user_agent: str
    notify_umo: str
    sites: tuple[str, ...]


@dataclass(slots=True)
class AccountResult:
    """Outcome of one check-in run for one account.

    Attributes:
        name: Account display name.
        ok: Whether the account is checked in today (success or already).
        repeat: Whether the site reported "already checked in" (code 1).
        detail: Human-readable summary for notifications.
    """

    name: str
    ok: bool
    repeat: bool
    detail: str


def complete_cookie_sites(cookie: str) -> list[str]:
    """List domains whose session-cookie keys are present in ``cookie``.

    Args:
        cookie: Raw Cookie header value.

    Returns:
        Domains that have a complete session key pair.
    """
    keys = {part.split("=", 1)[0].strip() for part in cookie.split(";") if "=" in part}
    return [
        domain
        for domain, required in SITE_COOKIE_KEYS.items()
        if all(key in keys for key in required)
    ]


def next_run_delay(now: datetime, hour: int) -> float:
    """Seconds until the next daily run at ``hour``:00 local time.

    Args:
        now: Current local time.
        hour: Hour of day (0-23) to run at.

    Returns:
        Seconds to sleep before the next run.
    """
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def load_accounts(raw_accounts: Any) -> list[GladosAccount]:
    """Validate the ``accounts`` config list into account objects.

    Args:
        raw_accounts: Value of the ``accounts`` config key.

    Returns:
        Parsed accounts (possibly with empty ``sites`` on bad cookies).

    Raises:
        GladosConfigError: If the value is not a list of objects.
    """
    if not isinstance(raw_accounts, list):
        raise GladosConfigError("GLaDOS 账户配置必须是数组")
    accounts: list[GladosAccount] = []
    for index, raw in enumerate(raw_accounts, start=1):
        if not isinstance(raw, dict):
            raise GladosConfigError(f"GLaDOS 账户 #{index} 配置格式错误")
        cookie = str(raw.get("cookie") or "").strip()
        if not cookie:
            raise GladosConfigError(f"GLaDOS 账户 #{index} 缺少 Cookie")
        name = str(raw.get("name") or "").strip() or f"账户{index}"
        sites = tuple(complete_cookie_sites(cookie))
        if not sites:
            logger.error(
                "GLaDOS account '%s' has no complete session cookie pair; "
                "glados.cloud needs gld:sess/gld:sess.sig and railgun.info "
                "needs koa:sess/koa:sess.sig. It will fail until the cookie "
                "is updated.",
                name,
            )
        accounts.append(
            GladosAccount(
                name=name,
                cookie=cookie,
                user_agent=str(raw.get("user_agent") or "").strip(),
                notify_umo=str(raw.get("notify_umo") or "").strip(),
                sites=sites,
            )
        )
    return accounts


def describe_site_error(code: Any, message: str) -> str:
    """Classify one domain's failed check-in into a short reason.

    Args:
        code: GLaDOS response code.
        message: GLaDOS response message.

    Returns:
        A short Chinese reason string.
    """
    lowered = (message or "").lower()
    if code == CODE_AUTOMATION or any(h in lowered for h in AUTOMATION_ERROR_HINTS):
        return (
            "被反自动化校验拦截：登录该账户的浏览器平台与签到 User-Agent 不一致，"
            "请在账户配置中把 user_agent 设为登录浏览器控制台的 navigator.userAgent"
        )
    if code == CODE_FAILURE and any(h in lowered for h in PERMISSION_ERROR_HINTS):
        return "Cookie 无效、已过期或不属于该站点"
    return f"code {code}：{message or '无消息字段'}"


def format_result(result: AccountResult) -> str:
    """Render an account result as the notification text.

    Args:
        result: Check-in outcome for one account.

    Returns:
        Notification text for the configured UMO.
    """
    if result.ok:
        icon = "☑️" if result.repeat else "✅"
        title = "今日已签到" if result.repeat else "签到成功"
        return f"{icon} GLaDOS {title} [{result.name}]\n{result.detail}"
    return f"❌ GLaDOS 签到失败 [{result.name}]\n{result.detail}"


async def glados_request(
    session: aiohttp.ClientSession,
    method: str,
    domain: str,
    path: str,
    cookie: str,
    user_agent: str,
    timeout: float,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one GLaDOS API request and return the parsed JSON body.

    Request headers and the compact JSON body mirror what the site frontend
    sends via axios (no Referer, charset-suffixed content type).

    Args:
        session: Shared aiohttp session.
        method: HTTP method ("GET" or "POST").
        domain: GLaDOS domain to talk to.
        path: API path, e.g. "/api/user/checkin".
        cookie: Raw Cookie header value.
        user_agent: User-Agent header value.
        timeout: Per-request timeout in seconds.
        json_body: Optional body serialized as compact JSON for POST.

    Returns:
        Parsed JSON response object.

    Raises:
        GladosApiError: On network errors, non-200 status, or invalid JSON.
    """
    headers = {
        "origin": f"https://{domain}",
        "accept": "application/json, text/plain, */*",
        "user-agent": user_agent,
        "cookie": cookie,
    }
    data = None
    if json_body is not None:
        headers["content-type"] = "application/json;charset=UTF-8"
        data = json.dumps(json_body, separators=(",", ":")).encode()

    url = f"https://{domain}{path}"
    try:
        async with session.request(
            method,
            url,
            headers=headers,
            data=data,
            timeout=timeout,
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise GladosApiError(f"HTTP {resp.status}：{text[:120]}")
            return json.loads(text)
    except GladosApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as error:
        raise GladosApiError(f"网络请求失败：{error}") from error


async def run_account_checkin(
    session: aiohttp.ClientSession,
    account: GladosAccount,
    timeout: float,
) -> AccountResult:
    """Check in one account on every domain its cookie can serve.

    The same cookie may hold session pairs for both domains; the account is
    considered checked in once any domain answers code 0 (success) or 1
    (already checked in). Status and points are then read from that domain.

    Args:
        session: Shared aiohttp session.
        account: Account to check in.
        timeout: Per-request timeout in seconds.

    Returns:
        The account result used for logging and notifications.
    """
    # GLaDOS compares the check-in request's UA platform with the browser
    # the account was logged in from, so the UA must follow the account.
    user_agent = account.user_agent or DEFAULT_USER_AGENT
    if not account.sites:
        return AccountResult(
            name=account.name,
            ok=False,
            repeat=False,
            detail=(
                "Cookie 缺少任一站点的完整会话字段：glados.cloud 需要 "
                "gld:sess 与 gld:sess.sig，railgun.info 需要 koa:sess 与 "
                "koa:sess.sig。请重新登录并复制完整 Cookie。"
            ),
        )

    errors: list[str] = []
    for domain in account.sites:
        try:
            data = await glados_request(
                session,
                "POST",
                domain,
                "/api/user/checkin",
                account.cookie,
                user_agent,
                timeout,
                json_body={"token": domain},
            )
        except GladosApiError as error:
            errors.append(f"{domain}：{error}")
            continue

        code = data.get("code")
        if code not in (CODE_SUCCESS, CODE_REPEAT):
            errors.append(
                f"{domain}：{describe_site_error(code, str(data.get('message', '')))}"
            )
            continue

        # Enrich the result with leftDays and total points; failures here do
        # not turn an accepted check-in into a failure.
        left_days = None
        total_points = None
        try:
            status_data = await glados_request(
                session,
                "GET",
                domain,
                "/api/user/status",
                account.cookie,
                user_agent,
                timeout,
            )
            raw_days = status_data.get("data", {}).get("leftDays")
            if raw_days is not None:
                left_days = int(float(raw_days))
        except (GladosApiError, TypeError, ValueError):
            logger.warning(
                "GLaDOS status query failed for account '%s' on %s",
                account.name,
                domain,
                exc_info=True,
            )
        try:
            points_data = await glados_request(
                session,
                "GET",
                domain,
                "/api/user/points",
                account.cookie,
                user_agent,
                timeout,
            )
            if points_data.get("points") is not None:
                total_points = int(float(points_data["points"]))
        except (GladosApiError, TypeError, ValueError):
            logger.warning(
                "GLaDOS points query failed for account '%s' on %s",
                account.name,
                domain,
                exc_info=True,
            )

        info = [f"站点 {domain}"]
        if code == CODE_SUCCESS:
            info.append(f"本次获得 {data.get('points', 0)} 积分")
        if left_days is not None:
            info.append(f"剩余 {left_days} 天")
        if total_points is not None:
            info.append(f"总积分 {total_points}")
        return AccountResult(
            name=account.name,
            ok=True,
            repeat=code == CODE_REPEAT,
            detail=" · ".join(info),
        )

    return AccountResult(
        name=account.name,
        ok=False,
        repeat=False,
        detail="\n".join(errors),
    )


class GladosPlugin(star.Star):
    """GLaDOS 每日定时签到。

    在配置的整点对每个账户执行 glados.cloud / railgun.info 签到，
    并把结果推送到账户配置的 UMO。
    命令：/glados checkin 手动签到绑定到当前会话的账户。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """Initialize the plugin from AstrBot configuration.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration from the WebUI.
        """
        super().__init__(context)
        self.config = config

        try:
            checkin_hour = int(config.get("checkin_hour", 9))
        except (TypeError, ValueError) as error:
            raise GladosConfigError("每天几点签到必须是整数") from error
        if not 0 <= checkin_hour <= 23:
            raise GladosConfigError("每天几点签到必须在 0-23 之间")
        self.checkin_hour = checkin_hour

        try:
            timeout = float(config.get("request_timeout", 30))
        except (TypeError, ValueError) as error:
            raise GladosConfigError("请求超时必须是数字") from error
        if timeout <= 0:
            raise GladosConfigError("请求超时必须大于 0")
        self.timeout = timeout

        self.accounts = load_accounts(config.get("accounts", []))
        self._running = False
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            "GLaDOS plugin loaded: %d account(s), daily check-in at %02d:00",
            len(self.accounts),
            self.checkin_hour,
        )

    async def terminate(self) -> None:
        """Stop the scheduler when AstrBot unloads the plugin."""
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass

    async def _scheduler_loop(self) -> None:
        """Sleep until the configured hour and run all check-ins, daily."""
        while True:
            delay = next_run_delay(datetime.now(), self.checkin_hour)
            logger.info(
                "GLaDOS next check-in in %.0f seconds (%02d:00 local)",
                delay,
                self.checkin_hour,
            )
            await asyncio.sleep(delay)
            try:
                await self.checkin_all()
            except Exception:
                logger.exception("GLaDOS scheduled check-in failed")

    async def checkin_all(
        self,
        accounts: list[GladosAccount] | None = None,
        notify: bool = True,
    ) -> list[AccountResult]:
        """Check in the given accounts once and optionally send notifications.

        Args:
            accounts: Accounts to check in; defaults to every configured
                account (used by the daily schedule).
            notify: Whether to push each result to the account's notify_umo.
                Manual runs disable this because the command reply already
                carries every bound account's result in the same session.

        Returns:
            Results for the checked accounts, in the given order.
        """
        results: list[AccountResult] = []
        accounts = accounts if accounts is not None else self.accounts
        if not accounts:
            return results
        async with aiohttp.ClientSession(trust_env=True) as session:
            for account in accounts:
                result = await run_account_checkin(session, account, self.timeout)
                results.append(result)
                logger.info(
                    "GLaDOS check-in '%s': %s",
                    account.name,
                    "ok" if result.ok else f"failed ({result.detail})",
                )
                if notify:
                    await self._notify(account, result)
        return results

    async def _notify(self, account: GladosAccount, result: AccountResult) -> None:
        """Send one account's result to its configured UMO, if any.

        Args:
            account: Account that produced the result.
            result: Check-in outcome to deliver.
        """
        umo = account.notify_umo
        if not umo:
            return
        try:
            sent = await self.context.send_message(
                umo,
                MessageChain().message(format_result(result)),
            )
        except Exception:
            logger.warning(
                "GLaDOS notification to %s failed for account '%s'",
                umo,
                account.name,
                exc_info=True,
            )
            return
        if not sent:
            logger.warning(
                "GLaDOS notification UMO %s matched no platform (account '%s')",
                umo,
                account.name,
            )

    @filter.command_group("glados")
    def glados(self) -> None:
        """GLaDOS 签到命令：/glados checkin 签到绑定到当前会话的账户。"""

    @glados.command("checkin")
    async def checkin(self, event: AstrMessageEvent):
        """Check in the accounts bound to this session's UMO immediately.

        Only accounts whose notify_umo equals this session's UMO are checked
        in, so a conversation can never trigger or see other accounts.

        Args:
            event: Incoming AstrBot message event.
        """
        bound = [
            account
            for account in self.accounts
            if account.notify_umo == event.unified_msg_origin
        ]
        if not bound:
            yield event.plain_result(
                "当前会话未绑定任何 GLaDOS 账户。请在插件配置中将账户的"
                "「签到通知 UMO」设为本会话（可在此会话发送 /sid 获取）。"
            )
            return
        if self._running:
            yield event.plain_result("GLaDOS 签到正在进行中，请稍后再试。")
            return
        self._running = True
        try:
            # The reply below already delivers each bound account's result in
            # this session, so skip the push notifications to avoid duplicates.
            results = await self.checkin_all(bound, notify=False)
        finally:
            self._running = False
        yield event.plain_result(
            "\n\n".join(format_result(result) for result in results)
        )
