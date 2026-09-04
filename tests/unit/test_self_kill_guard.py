"""Regression guard for the 2026-09-03 agent self-kill outage.

Forensics — production ``mclaw-v2`` on 115.159.191.117:

* A user sent the single word ``restart`` in the web chat. The agent
  read it as "restart the service" and issued
  ``run_shell: kill 2385810 2>/dev/null; sleep 5; pgrep -af "mclaw serve"``
  — 2385810 being the backend's *own* PID. ``RiskIntentGate`` was in
  ``trust_mode`` and waved it through.
* SIGTERM drove graceful shutdown to completion (uvicorn unbound the
  port, 19 plugins unloaded) but one non-daemon
  ``_connection_worker_thread`` never exited, leaving the process as a
  husk. systemd only watches MainPID, saw ``active``, and never fired
  ``Restart=always``. nginx served 502 for 23.5 hours.

Defence in depth, three layers — this file pins layer 1:

1. ``check_self_kill`` refuses the command mechanically (PID identity,
   not intent inference). Tested here.
2. ``arm_signal_force_exit_watchdog`` gives the SIGINT/SIGTERM path the
   ``os._exit(0)`` net that ``/api/shutdown`` already had, so a hung
   non-daemon thread can no longer produce a husk process.
3. An external systemd timer probes ``/api/health`` and restarts on
   three consecutive failures, because a live PID does not imply a live
   service.
"""

from __future__ import annotations

import os

import pytest

from mclaw.tools.terminal import check_self_kill


def _me() -> int:
    return os.getpid()


class TestRefusesSelfKill:
    """Commands that would terminate this very process must be refused."""

    def test_verbatim_incident_command(self):
        """The exact command from the 2026-09-03 outage."""
        cmd = f'kill {_me()} 2>/dev/null; sleep 5; pgrep -af "mclaw serve" || echo "进程已停止"'
        reason = check_self_kill(cmd)
        assert reason is not None
        # The refusal must tell the agent what to do instead, or it will
        # simply retry with a variant.
        assert "systemctl restart" in reason

    @pytest.mark.parametrize(
        "template",
        [
            "kill {pid}",
            "kill -9 {pid}",
            "kill -TERM {pid}",
            "sudo kill {pid}",
            "echo starting; kill {pid}",
            "true && kill {pid}",
            "false || kill {pid}",
        ],
    )
    def test_self_pid_variants(self, template):
        assert check_self_kill(template.format(pid=_me())) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            'pkill -f "mclaw serve"',
            'pkill -f "python.*scheduler"',
            "pkill -f uvicorn",
            "killall mclaw",
        ],
    )
    def test_name_matching_kills(self, cmd):
        """``pkill``/``killall`` can't be resolved to PIDs ahead of time,
        so any pattern touching our own process names is refused."""
        assert check_self_kill(cmd) is not None

    def test_parent_pid_is_also_fatal(self):
        """Killing the parent takes the backend down just as dead."""
        ppid = os.getppid()
        if ppid <= 1:
            pytest.skip("no meaningful parent pid in this environment")
        assert check_self_kill(f"kill {ppid}") is not None


class TestRefusesKillingOtherBackendProcesses:
    """The bypass found on 2026-09-04 while verifying the fix.

    ``_self_pids()`` only knows "me". But an agent can ``run_shell`` a
    child script and have *that* script kill the backend — different
    process, PID never matches, guard silently bypassed. I hit exactly
    this during verification and took production down for ~30s.

    ``_pid_is_mclaw_backend`` closes it by resolving the PID's cmdline
    instead of comparing against our own PID.
    """

    def test_kill_of_a_backend_pid_is_refused(self, monkeypatch):
        """A PID that isn't ours, but *is* a mclaw backend, must be refused."""
        import mclaw.tools.terminal as term

        monkeypatch.setattr(term, "_pid_is_mclaw_backend", lambda pid: pid == 424242)
        assert check_self_kill("kill 424242") is not None
        assert check_self_kill("kill -9 424242") is not None

    def test_unrelated_pid_still_allowed(self, monkeypatch):
        """Guard must stay narrow — killing a non-mclaw process is fine."""
        import mclaw.tools.terminal as term

        monkeypatch.setattr(term, "_pid_is_mclaw_backend", lambda pid: pid == 424242)
        assert check_self_kill("kill 555555") is None

    def test_unreadable_pid_does_not_raise(self, monkeypatch):
        """/proc unreadable (Windows, permissions, dead pid) must not throw —
        a crashing guard would break every run_shell call on the box."""
        import mclaw.tools.terminal as term

        assert term._pid_is_mclaw_backend(9999999) is False
        assert check_self_kill("kill 9999999") is None


class TestAllowsLegitimateCommands:
    """The guard must not become a nuisance — false positives would push
    users to disable it, which is worse than not having it."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "kill 99999",
            "kill -9 99999",
            "pkill -f chrome",
            "killall firefox",
            "ls -la",
            "git commit -m 'kill the flaky test'",
            "systemctl restart mclaw-v2",
            "sudo systemctl restart mclaw-v2",
            "grep -r 'killall' src/",
        ],
    )
    def test_not_refused(self, cmd):
        assert check_self_kill(cmd) is None

    def test_signal_number_not_mistaken_for_pid(self):
        """``kill -9 <other>``: the ``9`` is a signal, not a PID. A naive
        int-scan that strips the minus sign would match a process whose
        PID happens to be 9 and refuse every ``kill -N`` on the box."""
        assert check_self_kill("kill -9 99999") is None
        assert check_self_kill("kill -15 99999") is None


class TestSignalForceExitWatchdog:
    """Layer 2: the SIGTERM path must arm the same net ``/api/shutdown`` has."""

    def test_arms_and_is_idempotent(self, monkeypatch):
        import mclaw.api.server as server

        monkeypatch.setattr(server, "_SIGNAL_FORCE_EXIT_TIMER", None)
        started = []

        class _FakeTimer:
            def __init__(self, interval, fn):
                self.interval = interval
                self.fn = fn
                self.name = ""
                self.daemon = False

            def start(self):
                started.append(self)

        monkeypatch.setattr("threading.Timer", _FakeTimer)

        server.arm_signal_force_exit_watchdog()
        server.arm_signal_force_exit_watchdog()  # duplicate signal

        assert len(started) == 1, "duplicate SIGTERM must not stack timers"
        timer = started[0]
        assert timer.daemon is True, "watchdog must not itself block exit"
        assert timer.interval > 0

        monkeypatch.setattr(server, "_SIGNAL_FORCE_EXIT_TIMER", None)

    def test_disabled_when_grace_is_zero(self, monkeypatch):
        """``shutdown_force_exit_grace_s=0`` is the documented diagnostic
        escape hatch; it must genuinely disarm the net."""
        import mclaw.api.server as server

        monkeypatch.setattr(server, "_SIGNAL_FORCE_EXIT_TIMER", None)
        monkeypatch.setattr(server, "_resolve_force_exit_grace_s", lambda: 0)
        started = []
        monkeypatch.setattr(
            "threading.Timer",
            lambda *a, **k: started.append(1),
        )

        server.arm_signal_force_exit_watchdog()

        assert not started
        assert server._SIGNAL_FORCE_EXIT_TIMER is None
