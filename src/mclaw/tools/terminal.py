"""
Terminal Session Manager — persistent shell sessions with background process support.

Inspired by Cursor's "terminal as file" abstraction:
- Each terminal session persists across multiple run_shell calls
- Working directory and environment variables carry over
- Long-running commands auto-background after block_timeout_ms
- Output streams to data/terminals/{id}.txt for async monitoring
"""

import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── 自杀防护 ──────────────────────────────────────────────────────────
# 2026-09-03 线上事故：用户在 web 聊天里只发了一句 "restart"，agent 把它
# 理解成"重启服务"，于是执行 run_shell: ``kill 2385810`` —— 正是后端自己的
# PID。trust_mode 下 RiskIntentGate 直接放行。SIGTERM 走完优雅关闭，端口
# 释放了，但残留的非 daemon 线程钉住解释器，进程壳子不退；systemd 只看
# MainPID 判 active，Restart=always 从不触发，服务静默宕了 23.5 小时。
#
# 这里做的是最后一道机械防线：不猜意图，只认 PID。命中自身进程树就拒绝，
# 并在错误信息里指明正确做法（systemctl / mclaw stop 由外部进程执行，
# 这样 systemd 才能正常接管重启）。

_KILL_CMD_RE = re.compile(
    r"(?:^|[;&|]|\$\(|`)\s*(?:sudo\s+)?(kill|pkill|killall)\b([^;&|`)]*)",
    re.IGNORECASE,
)


def _self_pids() -> set[int]:
    """自身 PID + 父 PID + 进程组，killing 任一都会打死后端。"""
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
    except Exception:
        pass
    try:
        pids.add(os.getpgrp())
    except (AttributeError, OSError):
        pass  # Windows 无进程组
    pids.discard(0)
    return pids


def _pid_is_mclaw_backend(pid: int) -> bool:
    """该 PID 是不是一个 MClaw 后端进程（不限于本进程）。

    为什么需要它：``_self_pids()`` 只认得"我自己"。但 agent 完全可能用
    ``run_shell`` 起一个子脚本、由子脚本去 kill 后端——那时执行 kill 的
    进程与后端是两个进程，PID 对不上，防护就被绕过了。
    （2026-09-04 加固验证时我自己就踩了这个洞，把生产打挂了 30 秒。）

    所以这里按 PID 反查 cmdline，认"这是个 mclaw 后端"而不是"这是我"。
    读不到就返回 False —— 防护宁可漏判也不能误伤正常的 kill。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, ValueError):
        return False  # 非 Linux、进程不存在、或无权限读
    return bool(re.search(r"mclaw\b.*\bserve\b|\bmclaw\.main\b", cmdline))



def check_self_kill(command: str) -> str | None:
    """命令若会打死 MClaw 后端，返回拒绝理由；否则 None。

    覆盖四类写法：
      * ``kill [-SIG] <self_pid>`` / ``kill -9 -<pgid>``  —— 目标是本进程
      * ``kill <backend_pid>`` —— 目标是别的 mclaw 后端进程（子脚本绕过路径）
      * ``pkill -f "mclaw serve"`` 之类按名字匹配、会波及后端的
      * ``killall mclaw``
    """
    for match in _KILL_CMD_RE.finditer(command):
        verb = match.group(1).lower()
        args = match.group(2)
        mine = _self_pids()

        if verb == "kill":
            # 取出所有数字实参（跳过 -9 / -TERM 这类信号flag）；
            # ``kill -9 -1234`` 里的 -1234 是进程组，同样致命。
            for token in re.findall(r"-?\d+", args):
                num = abs(int(token))
                # 纯信号编号（kill -9）不会是 PID，但 abs 后可能撞上小 PID；
                # 用原始 token 判断：带 - 且值 <= 64 视为信号。
                if token.startswith("-") and num <= 64:
                    continue
                # 两条判据：是我自己，或者是任何一个 mclaw 后端进程。
                # 后者覆盖"agent 起子脚本、子脚本 kill 后端"的绕过路径。
                if num in mine or _pid_is_mclaw_backend(num):
                    return (
                        f"拒绝执行：该命令会终止 MClaw 后端进程 (PID {num})。"
                        f"服务自杀后 systemd 可能因残留线程判进程仍存活而不重启，"
                        f"导致服务静默宕机。如需重启，请在后端进程之外执行 "
                        f"`sudo systemctl restart mclaw-v2`（Linux 生产环境）。"
                    )
        else:
            # pkill/killall 按名字匹配，无法预判命中哪些 PID，
            # 只要模式沾上 mclaw/serve 就一律拒绝。
            if re.search(r"mclaw|\bserve\b|uvicorn|python", args, re.IGNORECASE):
                return (
                    f"拒绝执行：`{verb}` 的匹配模式可能命中 MClaw 后端进程，"
                    f"造成服务自杀且无法自动恢复。如需重启，请在后端进程之外执行 "
                    f"`sudo systemctl restart mclaw-v2`（Linux 生产环境）。"
                )
    return None



@dataclass
class ShellResult:
    """Result from a terminal command execution."""

    returncode: int
    stdout: str
    stderr: str
    backgrounded: bool = False
    terminal_file: str | None = None
    pid: int | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout + (f"\n{self.stderr}" if self.stderr else "")


@dataclass
class TerminalSession:
    """A persistent terminal session that maintains state across commands."""

    id: int
    cwd: str
    namespace: str = "default"
    env: dict = field(default_factory=dict)
    execution_env_spec: Any = None
    last_command: str | None = None
    last_exit_code: int | None = None
    _bg_process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _bg_task: asyncio.Task | None = field(default=None, repr=False)
    _started_at: float | None = field(default=None, repr=False)

    def _get_terminal_dir(self) -> Path:
        from ..config import settings

        terminal_dir = Path(settings.mclaw_home) / "data" / "terminals"
        terminal_dir.mkdir(parents=True, exist_ok=True)
        return terminal_dir

    @property
    def output_file(self) -> Path:
        namespace_hash = hashlib.sha256(self.namespace.encode("utf-8")).hexdigest()[:12]
        return self._get_terminal_dir() / f"{namespace_hash}-{self.id}.txt"

    def _write_header(self, pid: int, command: str) -> None:
        self._started_at = time.time()
        header = (
            f"---\n"
            f"pid: {pid}\n"
            f"cwd: {self.cwd}\n"
            f"last_command: {command}\n"
            f"running_for_ms: 0\n"
            f"---\n"
            f"$ {command}\n\n"
        )
        self.output_file.write_text(header, encoding="utf-8")

    def _update_running_time(self) -> None:
        if not self.output_file.exists() or self._started_at is None:
            return
        elapsed_ms = int((time.time() - self._started_at) * 1000)
        try:
            content = self.output_file.read_text(encoding="utf-8")
            import re

            content = re.sub(
                r"running_for_ms: \d+",
                f"running_for_ms: {elapsed_ms}",
                content,
                count=1,
            )
            self.output_file.write_text(content, encoding="utf-8")
        except Exception:
            pass

    def _write_footer(self, exit_code: int) -> None:
        elapsed_ms = int((time.time() - self._started_at) * 1000) if self._started_at else 0
        footer = f"\n---\nexit_code: {exit_code}\nelapsed_ms: {elapsed_ms}\n---\n"
        try:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(footer)
        except Exception:
            pass

    def _append_output(self, text: str) -> None:
        try:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _decode_output(self, data: bytes) -> str:
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            if sys.platform == "win32":
                try:
                    import ctypes

                    oem_cp = ctypes.windll.kernel32.GetOEMCP()
                    return data.decode(f"cp{oem_cp}", errors="replace")
                except Exception:
                    pass
            return data.decode("utf-8", errors="replace")

    async def execute(
        self,
        command: str,
        block_timeout_ms: int = 30000,
        working_directory: str | None = None,
    ) -> ShellResult:
        """Execute a command in this terminal session.

        If the command completes within block_timeout_ms, returns the result directly.
        Otherwise, the command continues in the background and output streams to
        the terminal file at data/terminals/{id}.txt.

        Uses streaming I/O (never communicate()) to avoid data loss on timeout.
        """
        if working_directory:
            self.cwd = str(Path(working_directory).resolve())

        from ..runtime_manager import build_user_subprocess_environment

        cmd_env = build_user_subprocess_environment(self.env)

        if self.execution_env_spec is not None:
            try:
                from ..runtime_manager import apply_execution_environment, ensure_execution_env

                cmd_env = apply_execution_environment(
                    cmd_env, ensure_execution_env(self.execution_env_spec)
                )
            except Exception as exc:
                logger.warning("Terminal falling back to shared agent Python env: %s", exc)
                try:
                    from ..runtime_manager import apply_agent_python_environment

                    cmd_env = apply_agent_python_environment(cmd_env)
                except Exception:
                    pass
        else:
            try:
                from ..runtime_manager import apply_agent_python_environment

                cmd_env = apply_agent_python_environment(cmd_env)
            except Exception:
                pass

        actual_command = self._prepare_command(command)
        self.last_command = command

        logger.info(f"Terminal {self.id}: executing '{command[:200]}'")

        process = await asyncio.create_subprocess_shell(
            actual_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=cmd_env,
        )

        pid = process.pid
        self._bg_process = process
        self._write_header(pid, command)

        def _make_bg_result(reason: str) -> ShellResult:
            return ShellResult(
                returncode=-1,
                stdout=(
                    f"{reason}\n"
                    f"Output streaming to: {self.output_file}\n"
                    f'Monitor with: read_file(path="{self.output_file}")\n'
                    f"The terminal file header has pid and running_for_ms "
                    f"(updated every 5s).\n"
                    f"When finished, a footer with exit_code and elapsed_ms "
                    f"will appear.\n"
                    f"Poll with exponential backoff to check progress.\n"
                    f'Kill if needed: run_shell(command="kill {pid}")'
                ),
                stderr="",
                backgrounded=True,
                terminal_file=str(self.output_file),
                pid=pid,
            )

        # Stream output into both in-memory buffer and terminal file.
        # Use asyncio.shield so the collector task survives a wait_for timeout
        # — this avoids data loss that would occur with communicate() + cancel.
        collected_stdout: list[str] = []
        collected_stderr: list[str] = []

        async def _collect_output() -> None:
            """Read stdout/stderr streams, appending to buffer + terminal file."""
            update_interval = 5.0
            last_update = time.time()

            try:
                if process.stdout:
                    async for line_bytes in process.stdout:
                        line = self._decode_output(line_bytes)
                        collected_stdout.append(line)
                        self._append_output(line)

                        now = time.time()
                        if now - last_update >= update_interval:
                            self._update_running_time()
                            last_update = now

                await process.wait()

                if process.stderr:
                    stderr_bytes = await process.stderr.read()
                    stderr = self._decode_output(stderr_bytes)
                    if stderr:
                        collected_stderr.append(stderr)
                        self._append_output(f"\n[stderr]:\n{stderr}")

                exit_code = process.returncode or 0
                self.last_exit_code = exit_code
                self._write_footer(exit_code)

            except Exception as e:
                logger.error(f"Terminal {self.id}: stream error: {e}")
                self._append_output(f"\n[ERROR]: {e}\n")
                self._write_footer(-1)
            finally:
                self._bg_process = None

        if block_timeout_ms == 0:
            self._bg_task = asyncio.create_task(_collect_output())
            return _make_bg_result(f"Command started in background (pid: {pid}).")

        collector_task = asyncio.create_task(_collect_output())

        try:
            # shield() prevents the collector task from being cancelled on timeout
            await asyncio.wait_for(
                asyncio.shield(collector_task),
                timeout=block_timeout_ms / 1000.0,
            )

            # Process completed within timeout
            stdout = "".join(collected_stdout)
            stderr = "".join(collected_stderr)
            exit_code = process.returncode or 0
            self.last_exit_code = exit_code
            self._bg_process = None

            return ShellResult(
                returncode=exit_code,
                stdout=stdout,
                stderr=stderr,
                pid=pid,
            )

        except TimeoutError:
            # Timeout — collector task continues running (protected by shield)
            self._bg_task = collector_task
            return _make_bg_result(
                f"Command did not complete within {block_timeout_ms}ms, "
                f"moved to background (pid: {pid})."
            )

        except asyncio.CancelledError:
            collector_task.cancel()
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            raise

    _cached_shell_tool: Any = None

    def _prepare_command(self, command: str) -> str:
        """Prepare command for execution (Windows encoding, etc.)."""
        if TerminalSession._cached_shell_tool is None:
            from .shell import ShellTool

            tool = ShellTool.__new__(ShellTool)
            tool._is_windows = sys.platform == "win32"
            tool._oem_encoding = None
            TerminalSession._cached_shell_tool = tool

        tool = TerminalSession._cached_shell_tool
        if tool._is_windows and tool._needs_powershell(command):
            return tool._wrap_for_powershell(command)
        elif tool._is_windows:
            return f"chcp 65001 >nul && {command}"
        return command


class TerminalSessionManager:
    """Manages multiple persistent terminal sessions."""

    def __init__(self, default_cwd: str | None = None, execution_env_spec: Any = None):
        self.sessions: dict[tuple[str, int], TerminalSession] = {}
        self.default_cwd = default_cwd or os.getcwd()
        self.execution_env_spec = execution_env_spec
        self._next_id = 1

    def get_or_create(self, session_id: int = 1, *, namespace: str = "default") -> TerminalSession:
        key = (namespace or "default", session_id)
        if key not in self.sessions:
            self.sessions[key] = TerminalSession(
                id=session_id,
                cwd=self.default_cwd,
                namespace=key[0],
                execution_env_spec=self.execution_env_spec,
            )
            if session_id >= self._next_id:
                self._next_id = session_id + 1
        return self.sessions[key]

    def list_sessions(self) -> list[dict]:
        result = []
        for _key, session in self.sessions.items():
            result.append(
                {
                    "id": session.id,
                    "namespace": session.namespace,
                    "cwd": session.cwd,
                    "last_command": session.last_command,
                    "last_exit_code": session.last_exit_code,
                    "has_background_process": session._bg_process is not None,
                }
            )
        return result

    async def execute(
        self,
        command: str,
        session_id: int = 1,
        namespace: str = "default",
        block_timeout_ms: int = 30000,
        working_directory: str | None = None,
    ) -> ShellResult:
        refusal = check_self_kill(command)
        if refusal:
            logger.warning(
                "[SelfKillGuard] refused command (session=%s, ns=%s): %s",
                session_id,
                namespace,
                command,
            )
            return ShellResult(returncode=1, stdout="", stderr=refusal)

        session = self.get_or_create(session_id, namespace=namespace)
        return await session.execute(
            command,
            block_timeout_ms=block_timeout_ms,
            working_directory=working_directory,
        )
