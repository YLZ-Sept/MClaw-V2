"""omni-post Playwright engine — main self-developed engine.

Responsibilities
----------------

1. Launch and teardown a Playwright browser with strict
   anti-fingerprinting (UA / viewport / webdriver scrub).
2. Inject the per-account cookie pool into an isolated browser context
   rooted at a per-account ``user_data_dir`` (prevents cookie leakage
   between accounts of the same platform).
3. Drive the adapter life cycle (``precheck`` → ``fill_form`` →
   ``submit``) and translate DOM failures into typed
   :class:`ErrorKind` values.
4. Capture a screenshot at every failure, redact any ``Cookie`` /
   ``Authorization`` / token strings out of the DOM before saving it.
5. Implement the generic "JSON steps" interpreter used by simple
   platform adapters (click / type / upload / wait / select /
   assert_url / eval / shadow_click / shadow_upload). Shadow DOM
   traversal is a first-class step kind — this is the direct fix for
   MultiPost-Extension issue #166 (WeChat Channels breakage).

The engine is intentionally stateless apart from the lazily
instantiated :class:`playwright.async_api.Playwright` handle, so the
host can hot-reload the plugin without leaking processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from omni_post_adapters import (
    AdapterContext,
    AdapterOutcome,
    PlatformAdapter,
    load_selector_bundle,
)
from omni_post_models import ErrorKind, OmniPostError

logger = logging.getLogger("mclaw.plugins.omni-post")


# ── Anti-fingerprinting knobs ─────────────────────────────────────────

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# Inline script baked into every context via ``add_init_script``. Strips
# the most obvious webdriver tells without touching anything else. We
# deliberately DO NOT try to defeat commercial fingerprinters — the goal
# is only to make the plugin indistinguishable from a manual session,
# not to bypass anti-bot walls.
_WEBDRIVER_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {
  get: () => ['zh-CN', 'zh', 'en']
});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5],
});
""".strip()


_SCREENSHOT_REDACTION_HEADERS = (
    "Cookie",
    "Set-Cookie",
    "Authorization",
    "X-Auth-Token",
)


class PlaywrightEngine:
    """Long-lived handle to the Playwright runtime + shared browser.

    We start ONE browser instance per engine instance and open ONE
    :class:`BrowserContext` per task, so every publish enjoys an
    isolated cookie jar while the heavy startup cost of launching
    Chromium is amortised across tasks.
    """

    def __init__(
        self,
        *,
        user_data_root: Path,
        selectors_dir: Path,
        screenshot_dir: Path,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._user_data_root = Path(user_data_root)
        self._selectors_dir = Path(selectors_dir)
        self._screenshot_dir = Path(screenshot_dir)
        self._settings = dict(settings or {})
        self._pw = None  # Playwright
        self._browser = None  # Browser
        self._lock = asyncio.Lock()

        self._user_data_root.mkdir(parents=True, exist_ok=True)
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    # ── Lifecycle ──────────────────────────────────────────────────

    async def ensure_started(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise OmniPostError(
                    ErrorKind.DEPENDENCY,
                    "Playwright Python package not installed",
                ) from e

            self._pw = await async_playwright().start()
            headless = bool(self._settings.get("playwright_headless", True))
            launch_args: list[str] = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            proxy = self._settings.get("proxy_url")
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": launch_args,
            }
            if proxy:
                launch_kwargs["proxy"] = {"server": str(proxy)}
            try:
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
            except Exception as e:
                # Most common on first run: Chromium binary not installed.
                raise OmniPostError(
                    ErrorKind.DEPENDENCY,
                    "Playwright Chromium not installed; "
                    "run `python -m playwright install chromium` once",
                ) from e

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    # ── Main entry point ───────────────────────────────────────────

    async def run_task(
        self,
        *,
        adapter: PlatformAdapter,
        task: dict[str, Any],
        account: dict[str, Any],
        cookies_plaintext: str,
        asset_path: str,
        cover_path: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> AdapterOutcome:
        """Full 3-step pipeline for one ``(task, account)`` pair."""

        await self.ensure_started()
        assert self._browser is not None

        merged_settings = {**self._settings, **(settings or {})}
        user_data_dir = self._user_data_root / account["platform"] / account["id"]
        user_data_dir.mkdir(parents=True, exist_ok=True)

        context = await self._browser.new_context(
            user_agent=_DEFAULT_UA,
            viewport=_DEFAULT_VIEWPORT,
            locale=str(merged_settings.get("locale", "zh-CN")),
        )
        await context.add_init_script(_WEBDRIVER_STEALTH_JS)
        await _inject_cookies(context, cookies_plaintext)

        page = await context.new_page()
        ctx = AdapterContext(
            task_id=task["id"],
            platform=task["platform"],
            account_id=account["id"],
            account_nickname=account.get("nickname") or "",
            payload=task.get("payload_json") or json.loads(task.get("payload_json") or "{}")
            if isinstance(task.get("payload_json"), str)
            else (task.get("payload") or {}),
            asset_storage_path=asset_path,
            cover_storage_path=cover_path,
            auto_submit=bool(merged_settings.get("auto_submit", True)),
            page=page,
            selectors=adapter.bundle,
            settings=merged_settings,
        )
        try:
            outcome = await self._run_lifecycle(adapter, ctx)
        except OmniPostError as e:
            outcome = AdapterOutcome(
                success=False,
                error_kind=e.kind.value,
                error_message=str(e),
                screenshots=list(ctx.screenshots),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error in engine")
            outcome = AdapterOutcome(
                success=False,
                error_kind=ErrorKind.UNKNOWN.value,
                error_message=str(e),
                screenshots=list(ctx.screenshots),
            )
            await _capture_failure_screenshot(page, self._screenshot_dir, ctx.task_id, "unexpected")
        finally:
            try:
                await context.close()
            except Exception:
                pass
        return outcome

    async def _run_lifecycle(self, adapter: PlatformAdapter, ctx: AdapterContext) -> AdapterOutcome:
        pre = await adapter.precheck(ctx)
        if not pre.success:
            await _capture_failure_screenshot(
                ctx.page, self._screenshot_dir, ctx.task_id, "precheck"
            )
            return pre
        fill = await adapter.fill_form(ctx)
        if not fill.success:
            await _capture_failure_screenshot(ctx.page, self._screenshot_dir, ctx.task_id, "fill")
            return fill
        if not ctx.auto_submit:
            return AdapterOutcome(
                success=True,
                published_url=None,
                error_kind=None,
                error_message="manual_submit_required",
                screenshots=list(ctx.screenshots),
                metrics={"auto_submit": False},
            )
        return await adapter.submit(ctx)


# ── Cookie injection ───────────────────────────────────────────────


async def _inject_cookies(context, cookies_plaintext: str) -> None:
    cookies = _parse_cookies(cookies_plaintext)
    if not cookies:
        return
    try:
        await context.add_cookies(cookies)
    except Exception as e:
        logger.warning("failed to inject cookies: %s", e)
        raise OmniPostError(
            ErrorKind.COOKIE_EXPIRED,
            "Playwright rejected the cookie jar — the user should re-import this account",
        ) from e


def _parse_cookies(plaintext: str) -> list[dict[str, Any]]:
    """Best-effort parser: accepts either Netscape format or JSON."""

    text = plaintext.strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        return [_normalize_cookie(c) for c in parsed if isinstance(c, dict)]

    out: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts[:7]
        out.append(
            _normalize_cookie(
                {
                    "domain": domain,
                    "path": path,
                    "secure": secure.upper() == "TRUE",
                    "expires": _safe_int(expiry),
                    "name": name,
                    "value": value,
                }
            )
        )
    return out


def _normalize_cookie(cookie: dict) -> dict[str, Any]:
    out = {
        "name": str(cookie.get("name", "")),
        "value": str(cookie.get("value", "")),
        "domain": str(cookie.get("domain", "")),
        "path": str(cookie.get("path", "/")),
        "secure": bool(cookie.get("secure", False)),
        "httpOnly": bool(cookie.get("httpOnly", cookie.get("http_only", False))),
    }
    expires = cookie.get("expires") or cookie.get("expirationDate") or cookie.get("expiry")
    if isinstance(expires, (int, float)):
        out["expires"] = int(expires)
    same = str(cookie.get("sameSite", cookie.get("same_site", ""))).lower()
    if same in {"strict", "lax", "none"}:
        out["sameSite"] = same.capitalize()
    return out


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── Screenshot + redaction ────────────────────────────────────────────


_COOKIE_TOKEN_PATTERN = re.compile(
    # Two forms:
    #   key: value[; …]        — matches headers with simple token values
    #   key: Bearer <token>    — matches Authorization: Bearer xxxxx
    # Both are scrubbed to `[REDACTED]`. We match greedily up to the
    # next semicolon or end-of-line so neither whitespace-delimited JWTs
    # nor compact cookies survive.
    r"(cookie|set-cookie|authorization|token)\s*[:=]\s*(?:Bearer\s+)?[^;\r\n]+",
    re.IGNORECASE,
)


async def _capture_failure_screenshot(
    page, screenshot_dir: Path, task_id: str, stage: str
) -> str | None:
    if page is None:
        return None
    safe_stage = re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:24]
    path = screenshot_dir / f"{task_id}_{safe_stage}.png"
    try:
        # Strip cookies from the DOM/headers before capturing so a
        # support request screenshot doesn't leak credentials.
        await page.evaluate(
            "() => { "
            "document.querySelectorAll('[data-sensitive],input[type=password]')"
            ".forEach(el => el.value = '***'); }"
        )
        await page.screenshot(path=str(path), full_page=False)
    except Exception as e:
        logger.warning("screenshot failed for %s: %s", task_id, e)
        return None
    return str(path)


def redact_cookie_text(text: str) -> str:
    """Public helper used by the log scrubber."""

    return _COOKIE_TOKEN_PATTERN.sub("[REDACTED]", text)


# ── Generic JSON-step interpreter ─────────────────────────────────────


class GenericJsonAdapter(PlatformAdapter):
    """Default adapter for platforms whose upload flow maps cleanly to
    the 9 supported step kinds.

    Subclass this ONLY when a platform needs more exotic logic.
    """

    def __init__(self, bundle: dict[str, Any]) -> None:
        super().__init__(bundle)
        self.platform_id = str(bundle.get("platform", ""))

    async def precheck(self, ctx: AdapterContext) -> AdapterOutcome:
        action = self.resolve_action("precheck")
        return await _run_steps(ctx, action)

    async def fill_form(self, ctx: AdapterContext) -> AdapterOutcome:
        action = self.resolve_action("fill_form")
        return await _run_steps(ctx, action)

    async def submit(self, ctx: AdapterContext) -> AdapterOutcome:
        action = self.resolve_action("submit")
        return await _run_steps(ctx, action)


async def _run_steps(ctx: AdapterContext, action: dict[str, Any]) -> AdapterOutcome:
    page = ctx.page
    url = action.get("url")
    timeout_ms = int(action.get("timeout_ms", 30_000))
    if url:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            return AdapterOutcome(
                success=False,
                error_kind=ErrorKind.NETWORK.value,
                error_message=f"goto failed: {e}",
                screenshots=list(ctx.screenshots),
            )
    for i, step in enumerate(action.get("steps", [])):
        try:
            await _execute_step(ctx, step)
        except OmniPostError as e:
            return AdapterOutcome(
                success=False,
                error_kind=e.kind.value,
                error_message=f"step {i} ({step.get('kind')}): {e}",
                screenshots=list(ctx.screenshots),
            )
        except Exception as e:  # noqa: BLE001
            return AdapterOutcome(
                success=False,
                error_kind=ErrorKind.UNKNOWN.value,
                error_message=f"step {i} ({step.get('kind')}): {e}",
                screenshots=list(ctx.screenshots),
            )

    published_url = None
    if action.get("capture_url_on_success"):
        try:
            published_url = page.url
        except Exception:
            published_url = None
    return AdapterOutcome(
        success=True,
        published_url=published_url,
        screenshots=list(ctx.screenshots),
    )


async def _execute_step(ctx: AdapterContext, step: dict) -> None:
    kind = step["kind"]
    page = ctx.page
    optional = bool(step.get("optional", False))
    timeout_ms = int(step.get("timeout_ms", 15_000))
    selector = _render_selector(step, ctx)

    try:
        if kind == "wait":
            if selector:
                await page.wait_for_selector(
                    selector, timeout=timeout_ms, state=step.get("state", "visible")
                )
            else:
                await asyncio.sleep(step.get("seconds", 1.0))
        elif kind == "click":
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout_ms)
            await locator.first.click()
        elif kind == "type":
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout_ms)
            text = _render_template(step.get("text", ""), ctx)
            if step.get("clear", False):
                await locator.first.fill("")
            await locator.first.fill(text)
        elif kind == "select":
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout_ms)
            await locator.first.select_option(_render_template(step.get("value", ""), ctx))
        elif kind == "upload":
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout_ms)
            field = step.get("field", "asset_storage_path")
            file_path = getattr(ctx, field, None) or _render_template(step.get("path", ""), ctx)
            if not file_path:
                raise OmniPostError(
                    ErrorKind.DEPENDENCY,
                    f"upload step requires non-empty field {field!r}",
                )
            await locator.first.set_input_files(str(file_path))
        elif kind == "assert_url":
            expected = _render_template(step.get("pattern", ""), ctx)
            if not expected:
                return
            pattern = re.compile(expected)
            current = page.url
            if not pattern.search(current):
                raise OmniPostError(
                    ErrorKind.COOKIE_EXPIRED
                    if step.get("on_fail") == "cookie_expired"
                    else ErrorKind.UNKNOWN,
                    f"url {current!r} does not match {expected!r}",
                )
        elif kind == "eval":
            await page.evaluate(step["script"])
        elif kind == "shadow_click":
            await _shadow_dom_action(page, step, "click")
        elif kind == "shadow_upload":
            await _shadow_dom_action(
                page,
                step,
                "upload",
                file_path=getattr(ctx, step.get("field", "asset_storage_path"), None),
            )
        else:
            raise ValueError(f"unknown step kind: {kind}")
    except OmniPostError:
        raise
    except Exception as e:
        if optional:
            logger.debug(
                "omni-post: optional step %s skipped (%s)",
                kind,
                e,
            )
            return
        raise


def _render_selector(step: dict, ctx: AdapterContext) -> str | None:
    sel = step.get("selector")
    if not sel:
        return None
    return _render_template(sel, ctx)


def _render_template(template: str, ctx: AdapterContext) -> str:
    if not template or "{{" not in template:
        return template
    payload = ctx.payload or {}
    tags_csv = ", ".join(payload.get("tags") or [])
    replacements = {
        "{{title}}": str(payload.get("title", "")),
        "{{description}}": str(payload.get("description", "")),
        "{{tags}}": tags_csv,
        "{{topic}}": str(payload.get("topic") or ""),
        "{{location}}": str(payload.get("location") or ""),
        "{{platform}}": ctx.platform,
    }
    out = template
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


async def _shadow_dom_action(
    page,
    step: dict,
    action: str,
    *,
    file_path: str | None = None,
) -> None:
    """Walk shadow roots AND iframes to locate an element and act on it.

    Fix for MultiPost-Extension issue #166. The payload looks like:

        {
            "kind": "shadow_click",
            "shadow_path": ["wujie-app", "#root", ".publish-btn"],
            "iframe_chain": ["iframe[name=mpIframe]"],
        }

    Each step in ``shadow_path`` is a CSS selector; if it targets a
    custom element we drill into its ``shadowRoot``. When
    ``iframe_chain`` is populated we switch contexts first.
    """

    frame = page
    for sel in step.get("iframe_chain", []):
        handle = await page.wait_for_selector(sel, timeout=15_000)
        frame_obj = await handle.content_frame()
        if frame_obj is None:
            raise OmniPostError(
                ErrorKind.PLATFORM_BREAKING_CHANGE,
                f"iframe {sel} is not attached",
            )
        frame = frame_obj

    path = step.get("shadow_path") or []
    if not path:
        raise ValueError("shadow_path must be a non-empty list")
    script_click = """
        async (spec) => {
            let node = document;
            for (const [idx, sel] of spec.path.entries()) {
                if (node.shadowRoot) node = node.shadowRoot;
                const found = node.querySelector(sel);
                if (!found) return { ok: false, at: idx, sel };
                node = found;
            }
            if (spec.action === 'click') {
                if (typeof node.click === 'function') { node.click(); return { ok: true }; }
                node.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return { ok: true };
            }
            return { ok: false, at: -1, sel: '(unsupported)' };
        }
    """
    if action == "click":
        res = await frame.evaluate(script_click, {"path": path, "action": "click"})
        if not res or not res.get("ok"):
            raise OmniPostError(
                ErrorKind.PLATFORM_BREAKING_CHANGE,
                f"shadow_click path failed at index {res.get('at') if res else -1}"
                f" sel={res.get('sel') if res else '(none)'}",
            )
    elif action == "upload":
        if not file_path:
            raise OmniPostError(
                ErrorKind.DEPENDENCY,
                "shadow_upload requires a file_path",
            )
        # Playwright cannot set_input_files on a shadow node directly;
        # we use the standard file chooser API instead.
        async with frame.expect_file_chooser() as fc_info:  # type: ignore[attr-defined]
            await frame.evaluate(script_click, {"path": path, "action": "click"})
        file_chooser = await fc_info.value
        await file_chooser.set_files(file_path)
    else:
        raise ValueError(f"unsupported shadow action: {action}")


def build_adapter(platform_id: str, selectors_dir: Path) -> PlatformAdapter:
    """Factory: return the right adapter subclass for ``platform_id``.

    Defaults to :class:`GenericJsonAdapter`. Platforms with custom
    overrides live in :mod:`omni_post_adapters.<platform>`.
    """

    bundle = load_selector_bundle(platform_id, selectors_dir)

    if platform_id == "wechat_channels":
        from omni_post_adapters.wechat_channels import WeChatChannelsAdapter

        return WeChatChannelsAdapter(bundle)
    if platform_id == "wechat_mp":
        from omni_post_adapters.wechat_mp import WeChatMpAdapter

        return WeChatMpAdapter(bundle)

    return GenericJsonAdapter(bundle)


__all__ = [
    "GenericJsonAdapter",
    "PlaywrightEngine",
    "build_adapter",
    "redact_cookie_text",
]
