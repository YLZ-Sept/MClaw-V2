"""
Global pytest fixtures for Mclaw test suite.
"""

from __future__ import annotations

import atexit
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Workaround: On Windows, platform._wmi_query() can hang when the WMI
# service is slow/unresponsive (e.g. after a crash).  Faker triggers this
# during pytest plugin collection via platform.system().  Pre-populate the
# uname cache so the real WMI call is never needed.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _orig_wmi = getattr(platform, "_wmi_query", None)
    if _orig_wmi is not None:
        platform._wmi_query = lambda *a, **k: ("10.0.26200", 1, "Multiprocessor Free", 0, 0)
        platform.system()  # populate cache
        platform._wmi_query = _orig_wmi  # restore

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# ---------------------------------------------------------------------------
# License enforcement off by default in tests.
#
# The suite exercises the product as a *licensed* install behaves. Without
# this, every ``create_app()`` test would boot into the unlicensed state and
# find plugins / skills / MCP / IM channels disabled. Tests that specifically
# cover license enforcement construct their own LicenseManager and never go
# through ``create_app``, so they are unaffected by this switch.
# ---------------------------------------------------------------------------
os.environ.setdefault("MCLAW_DISABLE_LICENSE", "1")

# ---------------------------------------------------------------------------
# 工作区隔离：测试绝不能写进真实的仓库目录
#
# 背景：``Settings.project_root`` 默认是 ``Path.cwd()``，而 ``mclaw.config.settings``
# 是**模块导入时**就建好的全局单例。pytest 从仓库根跑 → 所有 ``<project_root>/*``
# 的写入全部落在真实仓库里：
#
#   * ``data/llm_debug/``   —— openai provider 的 empty-response dump
#                              （历史留痕：``data/llm_debug/empty_response_test_*.json``）
#   * ``data/agent.db``     —— 与**正在运行的服务**抢同一个 SQLite 文件
#   * ``logs/``             —— ``mclaw/main.py`` 模块级的 ``setup_logging()`` 绑的
#                              RotatingFileHandler
#   * ``identity/``         —— ``build_system_prompt`` 的 compile 缓存（真实部署状态）
#
# ``data_dir`` / ``db_full_path`` / ``log_dir_path`` / ``identity_path`` 都是
# ``project_root`` 的派生属性，所以改这一个字段即可全覆盖。
#
# 时序要求：必须在**任何 test 模块 import ``mclaw.*`` 之前**执行——尤其是
# ``mclaw.main``（它的模块级 ``setup_logging`` 会立刻把 FileHandler 绑到真实
# ``logs/``，之后再改 settings 已经晚了）。conftest 的顶层代码先于 test 模块
# 的收集导入执行，所以放在这里是安全的。
#
# 与仓库既有 idiom 一致：多个测试自己就在用
# ``monkeypatch.setattr(settings, "project_root", tmp_path)``（见
# tests/api/test_p97_beta_smoke.py、tests/component/test_selfcheck.py 等），
# 这里只是把同一件事提到会话级统一做掉。
#
# ⚠️ ``project_root`` 同时承载**只读的项目资源**（``skills/``、``mcps/``、
# ``specs/``、``prompts/``）和**可写状态**（``data/``、``logs/``、``identity/``）。
# 改一个字段会把两者一起搬走——而测试读项目资源是合理的
# （``skill_manager`` 是 ``load_all(settings.project_root)``，项目级技能目录就解析
# 成 ``<project_root>/skills``；搬走后 ``/api/skills/content/...`` 直接 404）。
# 所以下面用符号链接把只读目录接回隔离根：读得到、写仍然落在隔离根。
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]

_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="mclaw-pytest-workspace-"))

# 只读的项目资源目录：链接进隔离根（``skills/`` 有 1000+ 文件 / 16MB，不适合每
# 次复制）。
_READONLY_PROJECT_DIRS = ("skills", "mcps", "specs", "prompts")


def _link_readonly_project_dirs(isolated_root: Path) -> None:
    """把仓库里的只读项目目录接进隔离根。

    ``os.symlink`` 在 Windows 上需要开发者模式；失败时退回复制（``skills/`` 约
    16MB，慢但正确）。两者都失败则跳过——相关测试会以"找不到技能"的形式失败，
    比静默写坏仓库可接受。
    """
    for name in _READONLY_PROJECT_DIRS:
        src = _REPO_ROOT / name
        if not src.is_dir():
            continue
        dst = isolated_root / name
        try:
            os.symlink(src, dst, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:
                shutil.copytree(src, dst)
            except OSError:
                pass


from mclaw.config import settings as _settings  # noqa: E402

_ORIGINAL_PROJECT_ROOT = _settings.project_root
_ORIGINAL_LOG_TO_FILE = _settings.log_to_file

for _sub in ("data", "logs", "identity"):
    (_ISOLATED_ROOT / _sub).mkdir(parents=True, exist_ok=True)

_link_readonly_project_dirs(_ISOLATED_ROOT)

_settings.project_root = _ISOLATED_ROOT
# 双保险：即使某个测试模块先 import 了 mclaw.main，setup_logging 也读这个开关。
_settings.log_to_file = False

# ``Settings.reload()``（``src/mclaw/config.py``）会新建一个 ``Settings()``——读的
# 是**真实** ``.env`` 与 ``Path.cwd()``——再把每个非持久化字段整个拷回这个单例。
# 于是任何走到 ``write_env`` → ``commit_env_config`` → ``reload()`` 的测试都会冲掉
# 上面两个隔离值。危害有不对称性、极难察觉：测试自己用 ``monkeypatch`` 设的字段由
# pytest 在 teardown 还原（伤害被掩盖），而这里**直接赋值**的 ``log_to_file`` /
# ``project_root`` 无人还原 —— 之后所有用例都带着文件日志开着、``project_root`` 指向
# 真实仓库在跑。故每次 reload 之后把隔离不变量重新钉回去。
# 注意：pydantic v2 的 ``BaseModel.__setattr__`` 拒绝非字段属性
# （``ValueError: "Settings" object has no field "reload"``），必须用
# ``object.__setattr__`` 绕过，否则 conftest 在导入期就会炸。
_original_settings_reload = _settings.reload


def _isolated_settings_reload() -> list[str]:
    changed = _original_settings_reload()
    for _field, _pinned in (("project_root", _ISOLATED_ROOT), ("log_to_file", False)):
        if getattr(_settings, _field) != _pinned:
            setattr(_settings, _field, _pinned)
    return changed


object.__setattr__(_settings, "reload", _isolated_settings_reload)

from tests.fixtures.mock_llm import MockBrain, MockLLMClient, MockResponse  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_workspace_guard():
    """哨兵：确认工作区隔离真的生效，并在会话结束时还原。

    隔离若是**静默**失效（例如某个 test 模块在 import 期就把
    ``settings.project_root`` 指回仓库根），测试会重新开始污染真实
    ``data/``/``logs/``/``identity/`` —— 这是最难发现的一类事故，必须在本次
    会话里立刻炸出来，而不是等几周后看到 `data/llm_debug/` 里多了一堆
    ``empty_response_test_*.json`` 才后知后觉。
    """
    assert _settings.project_root == _ISOLATED_ROOT, (
        f"工作区隔离未生效：settings.project_root={_settings.project_root!r}，"
        f"期望 {_ISOLATED_ROOT!r}。"
    )
    assert _settings.data_dir != _REPO_ROOT / "data", "data_dir 仍指向真实仓库 data/"
    assert _settings.log_dir_path != _REPO_ROOT / "logs", "log_dir_path 仍指向真实仓库 logs/"
    try:
        yield
    finally:
        _settings.project_root = _ORIGINAL_PROJECT_ROOT
        _settings.log_to_file = _ORIGINAL_LOG_TO_FILE
        _settings.__dict__.pop("reload", None)  # 撤销上面的 reload 包装
        # 隔离根里有 docs 副本、SQLite 库与日志，留着会随每次 pytest 运行累积。
        # 想留存排障可设 MCLAW_KEEP_TEST_WORKSPACE=1。
        if not os.environ.get("MCLAW_KEEP_TEST_WORKSPACE"):
            _remove_isolated_root()


def _remove_isolated_root(*, retries: int = 1) -> None:
    """尽力删除隔离根，**先摘掉只读目录的符号链接**。

    顺序至关重要：必须先 unlink/rmdir 那些链接，否则 ``rmtree`` 有可能顺着链接
    递归进真实的 ``skills/`` 并把仓库删掉。（``os.path.islink`` 对 junction 返回
    False，所以这里只认自己用 ``os.symlink`` 建的链接，并用 ``is_symlink`` 判断。）

    已知局限：测试跑完时 ``data/agent.db``、``data/memory/mclaw.db`` 等仍被
    aiosqlite 线程占着，Windows 上要等**进程完全退出**才释放——而 ``atexit`` 跑在
    daemon 线程被回收**之前**，所以重试也删不掉，会在 ``%TEMP%`` 留一个小目录
    （只剩几个 DB/日志文件，符号链接已摘走，不再有 16MB 的 skills 副本）。
    这是有意的取舍：宁可在临时目录留点残渣，也不去猜哪些连接该关。
    """
    for name in _READONLY_PROJECT_DIRS:
        link = _ISOLATED_ROOT / name
        try:
            if link.is_symlink():
                try:
                    os.unlink(link)  # POSIX 目录符号链接
                except OSError:
                    os.rmdir(link)  # Windows 目录符号链接
        except OSError:
            pass

    for attempt in range(retries):
        shutil.rmtree(_ISOLATED_ROOT, ignore_errors=True)
        if not _ISOLATED_ROOT.exists():
            return
        if attempt + 1 < retries:
            time.sleep(0.4)


atexit.register(_remove_isolated_root, retries=3)


@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    """A fresh MockLLMClient with an empty response queue."""
    client = MockLLMClient()
    client.set_default_response("Default mock response")
    return client


@pytest.fixture
def mock_brain(mock_llm_client: MockLLMClient) -> MockBrain:
    """A MockBrain backed by the mock_llm_client fixture."""
    return MockBrain(mock_llm_client)


@pytest.fixture
def test_session():
    """A clean test Session with no messages."""
    from tests.fixtures.factories import create_test_session

    return create_test_session()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A temporary workspace directory with standard subdirs."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memory").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "identity").mkdir()
    return tmp_path


@pytest.fixture
def test_settings(tmp_workspace: Path):
    """Test-specific Settings pointing to temp dirs, no external dependencies.

    隔离靠下面显式传的 ``project_root`` —— 不要再设 ``MCLAW_PROJECT_ROOT``：
    ``Settings`` 没有给该字段配 alias，pydantic-settings 只认 ``PROJECT_ROOT``，
    所以 ``MCLAW_PROJECT_ROOT`` 从来没人读过（会话级隔离见文件顶部的
    ``_ISOLATED_ROOT``）。
    """
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-placeholder")

    from mclaw.config import Settings

    settings = Settings(
        project_root=tmp_workspace,
        database_path=str(tmp_workspace / "data" / "agent.db"),
        log_dir=str(tmp_workspace / "logs"),
        log_level="WARNING",
        max_iterations=10,
    )
    yield settings


@pytest.fixture
def mock_response_factory():
    """Factory fixture for creating MockResponse instances."""

    def _create(
        content: str = "",
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
    ) -> MockResponse:
        return MockResponse(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )

    return _create


# ---------------------------------------------------------------------------
# Helpers for the (text, ConfigHint | None) tuple return type introduced by
# the web_search provider refactor (see src/mclaw/tools/tool_hints.py).
#
# Existing tests that only care about the LLM-facing text can use these to
# stay readable instead of repeating ``text, _ = await executor.execute_tool(...)``
# everywhere. New tests that DO care about the hint (e.g. the
# WebSearchHandler tests) should unpack the tuple directly.
# ---------------------------------------------------------------------------


async def call_tool_text(executor, tool_name, tool_input, **kwargs) -> str:
    """Run ``executor.execute_tool`` and return only the LLM-facing text.

    Drops the ``ConfigHint`` part of the tuple. Use in tests that pre-date
    the hint side-channel and only assert on the result string.
    """
    text, _hint = await executor.execute_tool(tool_name, tool_input, **kwargs)
    return text


async def call_tool_with_policy_text(
    executor, tool_name, tool_input, policy_result, **kwargs
) -> str:
    """Run ``execute_tool_with_policy`` and return only the text portion."""
    text, _hint = await executor.execute_tool_with_policy(
        tool_name, tool_input, policy_result, **kwargs
    )
    return text


@pytest.fixture
def call_tool_text_helper():
    """Fixture wrapper around :func:`call_tool_text` for tests that prefer DI."""
    return call_tool_text


# ---------------------------------------------------------------------------
# 全局禁用桌面通知 - 防止 pytest 误弹 Windows Toast / macOS / Linux 系统通知
#
# 背景：tests/unit/test_scheduler_executor_status.py 等测试通过 ``trigger_now``
# 真实执行 ``TaskExecutor.execute``，链路会触达 ``_send_end_notification``，
# 后者会调用 ``notify_task_completed_async`` 真去 PowerShell 弹 Toast。
# 表象是每跑一次 scheduler 测试就在桌面右下角连续弹好几条
# "✅ Mclaw 任务完成 / daily research"，体感极差且查不到源头。
#
# 这里在 autouse fixture 中把整个 desktop_notify 模块的发送函数替换成 no-op，
# 同时把 settings.desktop_notify_enabled 关掉作为双保险。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _disable_desktop_notifications(monkeypatch):
    """禁止测试过程中弹出真实的桌面通知。

    Phase 2 commit 11 把 ``desktop_notify`` 模块从 ``mclaw.core``
    搬到了 ``mclaw.agent``，旧路径仅保留 re-export shim。要让
    no-op 保护无论调用方走哪条 import 路径都生效，需要同时
    monkeypatch 两个模块的 send/notify 函数名字。
    """
    # fail-soft：模块未导入时静默跳过（pytest collection 阶段可能还没 import）
    modules: list = []
    try:
        from mclaw.agent import desktop_notify as _dn_core

        modules.append(_dn_core)
    except Exception:
        pass
    try:
        from mclaw.agent import desktop_notify as _dn_agent

        modules.append(_dn_agent)
    except Exception:
        pass

    if not modules:
        return

    async def _noop_async(*_args, **_kwargs):
        return False

    def _noop_sync(*_args, **_kwargs):
        return False

    for _dn in modules:
        monkeypatch.setattr(_dn, "send_desktop_notification", _noop_sync, raising=False)
        monkeypatch.setattr(_dn, "send_desktop_notification_async", _noop_async, raising=False)
        monkeypatch.setattr(_dn, "notify_task_completed", _noop_sync, raising=False)
        monkeypatch.setattr(_dn, "notify_task_completed_async", _noop_async, raising=False)

    # 双保险：把 settings.desktop_notify_enabled 也置为 False，覆盖任何动态导入
    try:
        from mclaw.config import settings as _settings

        monkeypatch.setattr(_settings, "desktop_notify_enabled", False, raising=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# C8b-1: 自动隔离 process-wide policy v2 singletons across tests.
#
# ``DeathSwitchTracker`` 累积 consecutive_denials；不重置时一个 test 的连续
# DENY 会让后续 test 误中 readonly_mode（StopIteration / 误 DENY）。
# ``SkillAllowlistManager`` 同理——一个 test 给 'foo' skill 加了 'bar' tool
# 之后下个 test 期待 'bar' is_allowed 仍 False。
#
# Bus（``UIConfirmBus``）已有 reset 路径但目前由各 test 自己管理；这里只统
# 一管理 C8b-1 新加的两个 singleton，避免污染其他 test 既有的 UIConfirmBus
# 显式 setup/teardown。
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _default_org_review_off(monkeypatch):
    """核心1/核心2: keep the new parent-executed review + rework loop OFF by
    default in tests.

    The review adds an extra LLM call per dispatched child and the rework loop
    can re-run a child, which would corrupt the deterministic canned-reply
    sequences (and call-count assertions) that the legacy orgs_v2 dispatch
    tests rely on. Production defaults the review ON (env unset). Tests that
    specifically exercise the review/rework behaviour opt in explicitly via
    ``monkeypatch.setenv("OPENAKITA_ORG_REVIEW_ENABLED", "1")``.

    The knobs are read dynamically (see ``_runtime_agent_pipeline_executor``)
    so a plain ``setenv`` here is honoured without import-time freezing.
    """
    monkeypatch.setenv("OPENAKITA_ORG_REVIEW_ENABLED", "0")
    # Legacy XML-dispatch fixtures remain useful migration coverage. Production
    # defaults this off; new structured-delegation tests explicitly remove it.
    monkeypatch.setenv("OPENAKITA_ORG_LEGACY_TEXT_DISPATCH", "1")


@pytest.fixture(autouse=True)
def _isolate_policy_v2_singletons():
    """每个 test 前后清空 DeathSwitch + SkillAllowlist singleton 状态。

    fail-soft：模块未导入时静默跳过（policy_v2 不在所有 test 范围内）。
    """
    try:
        from mclaw.core.policy_v2.death_switch import (
            reset_death_switch_tracker,
        )
        from mclaw.core.policy_v2.session_allowlist import (
            reset_session_allowlist_manager,
        )
        from mclaw.core.policy_v2.skill_allowlist import (
            reset_skill_allowlist_manager,
        )
    except Exception:
        yield
        return

    # 用 reset_*_singleton() 而非 .reset() / .clear()，因为 .reset() 故意保留
    # ``total_denials`` 作为 lifetime 计数（与 v1 parity）；test 之间需要完全
    # 隔离否则一个测试的 10 个 deny 会被下个测试看到。
    # C8b-3: SessionAllowlistManager 同样是 process-wide ephemeral，必须按 test
    # 隔离——否则一个 test ``apply_resolution(allow_session)`` 的副作用会让
    # 下一个 test 期待"该工具仍需 confirm"的断言失败。
    reset_death_switch_tracker()
    reset_skill_allowlist_manager()
    reset_session_allowlist_manager()
    try:
        yield
    finally:
        reset_death_switch_tracker()
        reset_skill_allowlist_manager()
        reset_session_allowlist_manager()
