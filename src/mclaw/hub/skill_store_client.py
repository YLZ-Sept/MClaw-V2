"""
SkillStoreClient — 多平台 AI Skill Store 客户端

支持的提供方：
- mclaw:     Mclaw 自建平台（默认，settings.hub_api_url）
- volces:    火山引擎 Find-Skill (skills.volces.com/v1)
- skillhub:  腾讯 SkillHub (skillhub.tencent.com)

通过 settings.hub_provider 切换。每个适配器负责将外部 API
映射为统一的 {items, total} 响应格式，安装流程则复用现有
的 git clone / GitHub ZIP fallback 通道。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

_RETRY_STATUS_CODES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_RATE_LIMIT_BACKOFF = 5.0


# ── HTTP helpers ──────────────────────────────────────────────────────────

async def _retry_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs,
) -> httpx.Response:
    last_exc: Exception | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            last_resp = resp
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", _RATE_LIMIT_BACKOFF))
                except (ValueError, TypeError):
                    retry_after = _RATE_LIMIT_BACKOFF
                wait = min(retry_after, 30.0) + random.uniform(0, 1)
                logger.warning("Rate limited (429) on %s, waiting %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code in _RETRY_STATUS_CODES and attempt < max_retries:
                wait = _BASE_BACKOFF * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Server error %d on %s, retry %d/%d in %.1fs",
                    resp.status_code, url, attempt + 1, max_retries, wait,
                )
                await asyncio.sleep(wait)
                continue
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries:
                wait = _BASE_BACKOFF * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Request to %s failed (%s), retry %d/%d in %.1fs",
                    url, type(e).__name__, attempt + 1, max_retries, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise
    if last_resp is not None:
        return last_resp
    if last_exc is None:
        raise RuntimeError("All retry attempts exhausted")
    raise last_exc


# ── Unified result types ──────────────────────────────────────────────────

def _normalize_item(raw: dict[str, Any], *, provider: str) -> dict[str, Any]:
    """Map a provider-specific skill object to the unified frontend format."""
    if provider == "volces":
        slug = raw.get("Slug", "")
        # slug format: "clawhub/owner/skill-name"
        parts = slug.split("/") if slug else []
        skill_name = parts[-1] if parts else raw.get("Name", "")
        owner = parts[1] if len(parts) >= 2 else ""
        source_repo = raw.get("SourceRepo", "")
        meta = raw.get("Metadata") or {}
        return {
            "id": raw.get("Id", ""),
            "name": raw.get("Name", skill_name),
            "description": raw.get("Description", ""),
            "display_description": meta.get("DisplayDescription", ""),
            "slug": slug,
            "owner": owner,
            "source_repo": source_repo,
            "download_count": raw.get("DownloadCount", 0),
            "rating": raw.get("EvaluationScore", 0),
            "created_at": raw.get("CreatedAt", ""),
            "updated_at": raw.get("UpdatedAt", ""),
            "keywords": raw.get("Keywords", ""),
            "files": meta.get("Files", []),
            "provider": "volces",
            "install_url": f"{source_repo}@{skill_name}" if source_repo else "",
        }
    elif provider == "mclaw":
        return {
            **raw,
            "provider": "mclaw",
            "display_description": raw.get("displayDescription", raw.get("description", "")),
        }
    # generic / future providers
    return {**raw, "provider": provider}


# ── Provider adapters ─────────────────────────────────────────────────────

class BaseProviderAdapter(ABC):
    """Provider adapter: maps external API ↔ unified internal format."""

    @abstractmethod
    def provider_name(self) -> str:
        ...

    @abstractmethod
    async def search(
        self, client: httpx.AsyncClient,
        query: str, category: str, trust_level: str,
        sort: str, page: int, limit: int,
    ) -> dict[str, Any]:
        """Return {"items": [...], "total": N}"""
        ...

    @abstractmethod
    async def get_detail(
        self, client: httpx.AsyncClient, skill_id: str,
    ) -> dict[str, Any]:
        ...

    async def download(
        self, client: httpx.AsyncClient, skill_id: str,
    ) -> bytes | None:
        """Download skill ZIP. Return None if not supported → falls back to git."""
        return None

    async def close(self) -> None:
        pass


class MclawAdapter(BaseProviderAdapter):
    """Mclaw 自建平台（保留现有 API 兼容）。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def provider_name(self) -> str:
        return "mclaw"

    async def search(self, client, query, category, trust_level, sort, page, limit):
        params: dict[str, Any] = {"page": str(page), "limit": str(limit), "sort": sort}
        if query:
            params["q"] = query
        if category:
            params["category"] = category
        if trust_level:
            params["trustLevel"] = trust_level
        resp = await _retry_request(client, "GET", "/skills", params=params)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", data.get("skills", []))
        total = data.get("total", data.get("totalCount", len(items)))
        normalized = [_normalize_item(it, provider="mclaw") for it in items]
        return {"items": normalized, "total": total}

    async def get_detail(self, client, skill_id):
        resp = await _retry_request(client, "GET", f"/skills/{skill_id}")
        resp.raise_for_status()
        return _normalize_item(resp.json(), provider="mclaw")

    async def download(self, client, skill_id):
        resp = await _retry_request(
            client, "GET", f"/skills/{skill_id}/download",
            follow_redirects=True, timeout=60.0,
        )
        if resp.status_code == 200 and len(resp.content) >= 22:
            return resp.content
        return None


class VolcesAdapter(BaseProviderAdapter):
    """火山引擎 Find-Skill 适配器 (skills.volces.com/v1)。"""

    BASE = "https://skills.volces.com/v1"

    def provider_name(self) -> str:
        return "volces"

    async def search(self, client, query, category, trust_level, sort, page, limit):
        params: dict[str, Any] = {"limit": str(limit)}
        if query:
            params["q"] = query
        # volces uses offset-based pagination
        offset = max(0, (page - 1) * limit)
        if offset > 0:
            params["offset"] = str(offset)
        if category:
            params["category"] = category
        if sort and sort != "installs":
            params["sort"] = sort

        resp = await _retry_request(client, "GET", "/skills", params=params)
        resp.raise_for_status()
        data = resp.json()
        raw_items = data.get("Skills", data.get("skills", []))
        total = data.get("Total", data.get("total", len(raw_items)))
        normalized = [_normalize_item(it, provider="volces") for it in raw_items]
        if sort == "installs" or not sort:
            normalized.sort(key=lambda x: x.get("download_count", 0), reverse=True)
        return {"items": normalized, "total": total}

    async def get_detail(self, client, skill_id):
        # Try detail endpoint first
        try:
            resp = await _retry_request(client, "GET", f"/skills/{skill_id}")
            if resp.status_code == 200:
                data = resp.json()
                # Handle volces' ResponseMetadata/Result wrapper
                result = data.get("Result") or data.get("Skill") or data
                if result and isinstance(result, dict) and result.get("Id"):
                    return _normalize_item(result, provider="volces")
        except Exception:
            pass

        # Fallback: search by the skill name embedded in the ID
        # volces skill IDs like "s-yeshhge0w0765hfoeg14" don't encode the name;
        # we rely on the caller providing install_url from search results instead.
        return _normalize_item({"Id": skill_id}, provider="volces")

    async def download(self, client, skill_id):
        # Find-Skill doesn't have a direct ZIP download endpoint;
        # the git fallback will clone from SourceRepo instead.
        return None


class SkillHubAdapter(BaseProviderAdapter):
    """腾讯 SkillHub 适配器 (api.skillhub.cn)。

    SkillHub 是腾讯云 Lighthouse 团队运营的国内 Skills 社区，
    提供 CDN 加速镜像、全中文界面和 TOP 50 精选榜单。
    匿名用户可搜索和安装 public skills。
    """

    BASE = "https://api.skillhub.cn"

    def provider_name(self) -> str:
        return "skillhub"

    async def search(self, client, query, category, trust_level, sort, page, limit):
        # SkillHub API only reliably supports: q, sort, limit.
        # Category and trust_level are applied client-side after fetching.
        # Pagination is also client-side (API has no page/offset).
        fetch_limit = 50  # Fetch more to enable client-side pagination + filtering
        params: dict[str, Any] = {"limit": str(fetch_limit)}
        if query:
            params["q"] = query
        if sort in ("downloads", "installs"):
            params["sort"] = "downloads"
        elif sort in ("rating", "score"):
            params["sort"] = "score"
        elif sort == "newest":
            params["sort"] = "updated_at"

        resp = await _retry_request(client, "GET", "/api/v1/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        raw_items = data.get("results", data.get("items", data.get("skills", [])))

        # Client-side category filter (SkillHub API doesn't enforce it server-side)
        if category:
            raw_items = [it for it in raw_items if it.get("category", "") == category]

        # Client-side trust_level → SkillHub "source" field
        if trust_level:
            source_map = {
                "official": "enterprise",
                "certified": "enterprise",
                "community": "clawhub",
            }
            target_source = source_map.get(trust_level)
            if target_source:
                raw_items = [it for it in raw_items if it.get("source", "") == target_source]

        normalized = [_normalize_skillhub(it) for it in raw_items]

        # Client-side sort
        if sort in ("installs", "downloads") or not sort:
            normalized.sort(key=lambda x: x.get("download_count", 0), reverse=True)
        elif sort == "rating":
            normalized.sort(key=lambda x: x.get("rating", 0), reverse=True)

        # Client-side pagination
        total = len(normalized)
        start = (page - 1) * limit
        paged = normalized[start:start + limit] if limit > 0 else normalized

        # Heuristic: when API returns a full page, more results likely exist
        if len(raw_items) >= fetch_limit:
            total = max(total, 25000)

        return {"items": paged, "total": total}

    async def get_detail(self, client, skill_id):
        # SkillHub doesn't expose a public detail endpoint; return minimal
        # data so the install flow can use the install_url from search results.
        return _normalize_skillhub({"slug": skill_id, "name": skill_id})

    async def download(self, client, skill_id):
        try:
            resp = await _retry_request(
                client, "GET", f"/api/v1/skills/{skill_id}/download",
                follow_redirects=True, timeout=60.0,
            )
            if resp.status_code == 200 and len(resp.content) >= 22:
                return resp.content
        except Exception:
            pass
        return None


def _normalize_skillhub(raw: dict[str, Any]) -> dict[str, Any]:
    """Map SkillHub's response format to unified fields."""
    ns = raw.get("namespace") or {}
    slug = raw.get("slug", "")
    # canonicalName is like "@owner/slug"; use it to build install_url
    canonical = ns.get("canonicalName", "")
    source = raw.get("source", "")
    name = raw.get("name", raw.get("displayName", slug))

    # Build install_url: for ClawHub source use the owner/slug convention,
    # letting git fallback clone from GitHub and extract the skill by name.
    # Format: "owner/repo@slug" where repo=slug for single-skill repos.
    if canonical:
        # "@owner/slug" → "owner/slug@slug"
        parts = canonical.lstrip("@").split("/")
        owner = parts[0] if parts else ""
        skill_slug = parts[-1] if len(parts) >= 2 else slug
        # For single-skill repos the repo name typically matches the skill slug
        install_url = f"{owner}/{skill_slug}@{skill_slug}"
    else:
        install_url = raw.get("homepage", "")

    return {
        # URL-safe: slug only (no @ or / that break URL routing like /api/hub/skills/{id}/install)
        "id": slug,
        "name": name,
        "description": raw.get("description", ""),
        "display_description": raw.get("summary", raw.get("description", "")),
        "slug": slug,
        "owner": ns.get("displayName", ns.get("handle", "")),
        "source_repo": f"github.com/{canonical.lstrip('@').replace('/', '/', 1)}" if source == "clawhub" and canonical else "",
        "download_count": raw.get("downloads", raw.get("installs", 0)),
        "rating": raw.get("score", 0),
        "created_at": raw.get("created_at", ""),
        "updated_at": raw.get("updated_at", ""),
        "keywords": "",
        "provider": "skillhub",
        "install_url": install_url,
        # Extra SkillHub-specific fields
        "version": raw.get("version", ""),
        "stars": raw.get("stars", 0),
        "source": source,
    }


# ── Adapter factory ──────────────────────────────────────────────────────

_ADAPTERS: dict[str, type[BaseProviderAdapter]] = {
    "mclaw": MclawAdapter,
    "volces": VolcesAdapter,
    "skillhub": SkillHubAdapter,
}


def _resolve_provider() -> str:
    """Return the effective hub provider from settings."""
    provider = getattr(settings, "hub_provider", None) or "mclaw"
    return provider.strip().lower()


def _resolve_base_url(provider: str) -> str:
    if provider == "volces":
        return VolcesAdapter.BASE
    if provider == "skillhub":
        return SkillHubAdapter.BASE
    return (getattr(settings, "hub_api_url", None) or "https://mclaw.ai/api").rstrip("/")


def _create_adapter(provider: str) -> BaseProviderAdapter:
    cls = _ADAPTERS.get(provider)
    if cls is None:
        logger.warning("Unknown hub provider %r, falling back to mclaw", provider)
        cls = MclawAdapter
    if provider == "volces":
        return cls()
    if provider == "skillhub":
        return cls()
    return cls(_resolve_base_url(provider))


# ── Public client ─────────────────────────────────────────────────────────

class SkillStoreClient:
    """多平台 Skill Store HTTP 客户端。

    通过 settings.hub_provider 切换后端：
    - "mclaw"   → Mclaw 自建平台
    - "volces"  → 火山引擎 Find-Skill
    - "skillhub" → 腾讯 SkillHub
    """

    def __init__(self, base_url: str | None = None, provider: str | None = None):
        self._provider_name = provider or _resolve_provider()
        self.base_url = base_url or _resolve_base_url(self._provider_name)
        self._adapter = _create_adapter(self._provider_name)
        self._client: httpx.AsyncClient | None = None

    @property
    def provider(self) -> str:
        return self._provider_name

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": f"Mclaw/{self._get_version()}"}
        # Only attach API keys for mclaw platform
        if self._provider_name == "mclaw":
            if settings.hub_api_key:
                headers["X-Akita-Key"] = settings.hub_api_key
            if settings.hub_device_id:
                headers["X-Akita-Device"] = settings.hub_device_id
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._auth_headers(),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        await self._adapter.close()

    @staticmethod
    def _get_version() -> str:
        try:
            from .._bundled_version import __version__
            return __version__
        except Exception:
            return "dev"

    # ── Delegated API ───────────────────────────────────────────────────

    async def search(
        self,
        query: str = "",
        category: str = "",
        trust_level: str = "",
        sort: str = "installs",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """搜索技能，返回 {"items": [...], "total": N}。"""
        client = await self._get_client()
        return await self._adapter.search(
            client, query=query, category=category,
            trust_level=trust_level, sort=sort, page=page, limit=limit,
        )

    async def get_detail(self, skill_id: str) -> dict[str, Any]:
        """获取技能详情。"""
        client = await self._get_client()
        return await self._adapter.get_detail(client, skill_id)

    # ── Install (shared logic) ──────────────────────────────────────────

    @staticmethod
    def _write_origin(skill_dir: Path, install_url: str) -> None:
        try:
            origin = {
                "source": install_url,
                "type": "platform_store",
                "installed_at": datetime.now(UTC).isoformat(),
            }
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                m = re.match(r"^---\s*\n(.*?)\n---", skill_md.read_text("utf-8"), re.DOTALL)
                if m:
                    import yaml
                    fm = yaml.safe_load(m.group(1)) or {}
                    if fm.get("version"):
                        origin["version"] = fm["version"]
            (skill_dir / ".mclaw-origin.json").write_text(
                json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (skill_dir / ".mclaw-source").write_text(install_url, encoding="utf-8")
        except Exception as e:
            logger.debug(f"Failed to write origin tracking: {e}")

    async def install_skill(
        self,
        install_url: str,
        target_dir: Path | None = None,
        *,
        skill_id: str | None = None,
    ) -> Path:
        """安装 Skill 到本地。

        流程：(1) 平台缓存 → (2) clawhub CLI (仅 SkillHub) → (3) git clone → (4) ZIP 镜像
        """
        if target_dir is None:
            target_dir = settings.skills_path

        target_dir.mkdir(parents=True, exist_ok=True)

        if "@" in install_url and "/" in install_url:
            repo_part, skill_name = install_url.rsplit("@", 1)
            if not repo_part.startswith("http"):
                repo_part = f"https://github.com/{repo_part}"
        else:
            repo_part = install_url
            skill_name = install_url.rsplit("/", 1)[-1]

        skill_dir = target_dir / skill_name
        if skill_dir.exists():
            logger.info(f"Skill {skill_name} already exists, updating...")
            shutil.rmtree(skill_dir)

        # Strategy 1: Download cached ZIP from platform
        if skill_id:
            try:
                client = await self._get_client()
                data = await self._adapter.download(client, skill_id)
                if data:
                    import io
                    import zipfile
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    abs_target = str(skill_dir.resolve()) + os.sep
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        for member in zf.namelist():
                            member_path = os.path.normpath(os.path.join(skill_dir.resolve(), member))
                            if not member_path.startswith(abs_target) and member_path != abs_target.rstrip(os.sep):
                                raise RuntimeError(f"Zip Slip detected: {member}")
                        zf.extractall(skill_dir)
                    if (skill_dir / "SKILL.md").exists():
                        self._write_origin(skill_dir, install_url)
                        logger.info(f"Installed skill from platform cache: {skill_name}")
                        return skill_dir
            except Exception as e:
                logger.debug(f"Platform cache download failed for {skill_id}: {e}")
                if skill_dir.exists():
                    shutil.rmtree(skill_dir, ignore_errors=True)

        if skill_dir.exists():
            shutil.rmtree(skill_dir, ignore_errors=True)

        # Strategy 2: for SkillHub, use the official clawhub CLI (CDN download)
        if self._provider_name == "skillhub" and skill_name:
            try:
                installed = await self._install_via_clawhub(skill_name, target_dir)
                if installed:
                    self._write_origin(skill_dir, install_url)
                    logger.info(f"Installed skill via clawhub: {skill_name} -> {skill_dir}")
                    return skill_dir
            except Exception as e:
                logger.debug(f"clawhub install failed for {skill_name}: {e}")
                if skill_dir.exists():
                    shutil.rmtree(skill_dir, ignore_errors=True)

        # Strategy 3: git clone fallback
        if repo_part and repo_part.startswith("http"):
            try:
                installed = await self._install_via_git(repo_part, skill_name, skill_dir)
                if installed:
                    self._write_origin(skill_dir, install_url)
                    logger.info(f"Installed skill via git: {skill_name} -> {skill_dir}")
                    return skill_dir
            except Exception as e:
                logger.debug(f"git clone failed for {skill_name}: {e}")
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)

        raise RuntimeError(
            f"Failed to install skill '{skill_name}': "
            "neither platform cache nor git clone succeeded"
        )

    @staticmethod
    async def _install_via_clawhub(skill_name: str, target_dir: Path) -> bool:
        """Install a skill via the official clawhub CLI (CDN download, no git needed)."""
        import shutil as _shutil

        npx = _shutil.which("npx")
        if npx is None:
            raise RuntimeError("npx not found in PATH")

        extra_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        target_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                npx, "--yes", "clawhub@latest", "install", skill_name,
                "--registry", SkillHubAdapter.BASE,
                "--dir", str(target_dir),
                "--no-input",
            ],
            capture_output=True, text=True, timeout=120, **extra_kwargs,
        )
        if result.returncode != 0:
            raise RuntimeError(f"clawhub install failed: {result.stderr or result.stdout}")

        skill_dir = target_dir / skill_name
        return skill_dir.is_dir() and (skill_dir / "SKILL.md").exists()

    @staticmethod
    async def _install_via_git(repo_url: str, skill_name: str, skill_dir: Path) -> bool:
        import tempfile

        git_exe = shutil.which("git")
        if git_exe is None:
            return await SkillStoreClient._install_via_zip_fallback(repo_url, skill_name, skill_dir)

        extra_kwargs: dict = {}
        if sys.platform == "win32":
            extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        tmp_parent = Path(tempfile.mkdtemp(prefix="mclaw_skill_"))
        tmp_dir = tmp_parent / "repo"
        try:
            result = subprocess.run(
                [git_exe, "clone", "--depth=1", repo_url, str(tmp_dir)],
                capture_output=True, text=True, timeout=30, **extra_kwargs,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr}")
            return SkillStoreClient._extract_skill_from_repo(tmp_dir, skill_name, skill_dir)
        except (subprocess.TimeoutExpired, Exception):
            logger.debug("git clone failed for %s, trying ZIP fallback", repo_url)
            return await SkillStoreClient._install_via_zip_fallback(repo_url, skill_name, skill_dir)
        finally:
            shutil.rmtree(str(tmp_parent), ignore_errors=True)

    @staticmethod
    async def _install_via_zip_fallback(repo_url: str, skill_name: str, skill_dir: Path) -> bool:
        import io
        import tempfile
        import urllib.request
        import zipfile

        m = re.match(r"https?://github\.com/([^/]+)/([^/.]+)", repo_url)
        if not m:
            raise FileNotFoundError(
                "git not found in PATH, and the repo URL is not a recognized GitHub URL "
                "for ZIP fallback. Please install Git (https://git-scm.com)."
            )

        owner, repo = m.group(1), m.group(2)
        mirrors = [
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
            f"https://gh-proxy.com/https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
            f"https://mirror.ghproxy.com/https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
            f"https://ghproxy.net/https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
            f"https://gh.api.99988866.xyz/https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
            f"https://gh.con.sh/https://github.com/{owner}/{repo}/archive/refs/heads/{{branch}}.zip",
        ]

        data: bytes | None = None
        last_err: Exception | None = None

        for branch in ("main", "master"):
            if data is not None:
                break
            for tpl in mirrors:
                url = tpl.format(branch=branch)
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mclaw"})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = resp.read()
                    break
                except Exception as e:
                    last_err = e

        if data is None:
            raise RuntimeError(
                f"Git is not installed, and ZIP download from GitHub also failed "
                f"for {owner}/{repo}. (Last error: {last_err})"
            )

        tmp_parent = Path(tempfile.mkdtemp(prefix="mclaw_skill_zip_"))
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    normalized = os.path.normpath(name)
                    if name.startswith("/") or name.startswith("\\") or normalized.startswith(".."):
                        raise RuntimeError(f"Zip Slip detected: {name}")
                zf.extractall(tmp_parent)

            children = list(tmp_parent.iterdir())
            extracted = [c for c in children if c.is_dir() and c.name != "repo"]
            if len(extracted) == 1:
                tmp_dir = extracted[0]
            elif (tmp_parent / "repo").exists():
                tmp_dir = tmp_parent / "repo"
            else:
                tmp_dir = tmp_parent

            return SkillStoreClient._extract_skill_from_repo(tmp_dir, skill_name, skill_dir)
        finally:
            shutil.rmtree(str(tmp_parent), ignore_errors=True)

    @staticmethod
    def _extract_skill_from_repo(tmp_dir: Path, skill_name: str, skill_dir: Path) -> bool:
        skill_md_at_root = tmp_dir / "SKILL.md"
        if skill_md_at_root.exists():
            shutil.copytree(str(tmp_dir), str(skill_dir))
            git_dir = skill_dir / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)
            return True

        candidates = [
            skill_name,
            f"skills/{skill_name}", f"tools/{skill_name}", f"packages/{skill_name}",
        ]
        seen: set[str] = set()
        for rel in candidates:
            rel_norm = rel.replace("\\", "/").strip("/")
            if not rel_norm or rel_norm in seen:
                continue
            seen.add(rel_norm)
            candidate = tmp_dir / rel_norm
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                shutil.copytree(str(candidate), str(skill_dir))
                return True

        for skill_md in tmp_dir.rglob("SKILL.md"):
            if skill_md.parent.name == skill_name:
                shutil.copytree(str(skill_md.parent), str(skill_dir))
                return True

        shutil.copytree(str(tmp_dir), str(skill_dir))
        git_dir = skill_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
        return True

    # ── Rating / Submit (mclaw-only for now) ──────────────────────────────

    async def rate(
        self, skill_id: str, score: int, comment: str = "", token: str = ""
    ) -> dict[str, Any]:
        if self._provider_name != "mclaw":
            return {"ok": False, "error": "Rating is only supported on Mclaw platform"}
        client = await self._get_client()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await _retry_request(
            client, "POST", f"/skills/{skill_id}/rate",
            json={"score": score, "comment": comment}, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def submit_repo(self, repo_url: str) -> dict[str, Any]:
        if self._provider_name != "mclaw":
            return {"ok": False, "error": "Submit is only supported on Mclaw platform"}
        client = await self._get_client()
        resp = await _retry_request(
            client, "POST", "/skills/submit-repo", json={"repoUrl": repo_url},
        )
        resp.raise_for_status()
        return resp.json()
