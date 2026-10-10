"""测试工作区隔离不变量。

背景：``Settings.project_root`` 默认 ``Path.cwd()``，而 ``mclaw.config.settings``
是**模块导入时**建好的全局单例。pytest 从仓库根跑时，所有 ``<project_root>/*``
的写入都会落进真实仓库——历史上造成过 ``data/llm_debug/empty_response_test_*.json``
之类的污染，更危险的是和**正在运行的服务**抢同一个 ``data/agent.db``。

``tests/conftest.py`` 在导入期把单例的 ``project_root`` 重定向到会话临时目录。
本文件把这个不变量钉死：一旦隔离被静默破坏（有人提前 import 了 ``mclaw.main``、
或在别处重新赋值了 ``project_root``），这里会立刻红，而不是等几周后看到真实
``data/`` 里多了一堆文件才后知后觉。
"""

from __future__ import annotations

from pathlib import Path

from mclaw.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestWorkspaceIsolation:
    def test_project_root_is_not_the_repo_root(self) -> None:
        assert settings.project_root != REPO_ROOT, (
            "settings.project_root 指向仓库根——conftest 的会话级隔离没生效，"
            "测试会开始污染真实目录。"
        )

    def test_derived_paths_stay_outside_the_repo(self) -> None:
        """``data_dir`` / ``db_full_path`` / ``log_dir_path`` / ``identity_path``
        都是 ``project_root`` 的派生属性，必须一并落在仓库外。"""
        for name, path in (
            ("data_dir", settings.data_dir),
            ("db_full_path", settings.db_full_path),
            ("log_dir_path", settings.log_dir_path),
            ("identity_path", settings.identity_path),
            ("selfcheck_dir", settings.selfcheck_dir),
        ):
            assert not path.is_relative_to(REPO_ROOT), (
                f"settings.{name} = {path} 落在仓库内（{REPO_ROOT}）"
            )

    def test_file_logging_is_disabled(self) -> None:
        """``mclaw/main.py`` 在模块层就调 ``setup_logging()``，读的是单例。
        隔离必须同时关掉文件日志，否则测试仍会往真实 ``logs/`` 写。"""
        assert settings.log_to_file is False

    def test_writes_land_in_the_isolated_root(self) -> None:
        """按生产代码的写法（``settings.project_root / "data" / ...``）落一次盘，
        确认真的写在了隔离根下，而不是仓库的 ``data/``。"""
        # 复刻 src/mclaw/llm/providers/openai.py::_dump_empty_response 的路径拼法
        debug_dir = settings.project_root / "data" / "llm_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        target = debug_dir / "isolation_probe.json"
        target.write_text('{"probe": true}', encoding="utf-8")

        assert target.is_file()
        assert not target.is_relative_to(REPO_ROOT)
        assert target.is_relative_to(settings.project_root)
