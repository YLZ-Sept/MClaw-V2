# Mclaw Bootstrap Resources

This directory is packaged into the Tauri desktop app and is intentionally
small. It bootstraps the mutable runtime environments under:

```text
~/.mclaw/runtime/app-venv
~/.mclaw/runtime/agent-venv
```

Expected packaged files:

- `manifest.json`: bootstrap metadata consumed by the Tauri runtime manager.
- `bin/uv` or `bin/uv.exe`: uv binary for creating venvs and installing wheels.
- `wheels/mclaw-<version>-py3-none-any.whl`: Mclaw wheel for app runtime.
- `wheelhouse/`: optional enterprise/offline dependency wheelhouse.
- `python/`: platform-specific standalone Python seed used to create the managed venvs.

`build/prepare_bootstrap_resources.py` defaults to a gitignored staging output
under `build/bootstrap-output` for local validation. CI/release and the local
desktop packaging scripts pass `--commit-resources` to write into this directory
intentionally. Do not commit generated Python, `bin/uv*`, or wheel files from a
local run unless you are updating tracked release bootstrap metadata on purpose.

The Python seed is a slim standalone interpreter, not a preinstalled Mclaw
environment. Application dependencies remain in the mutable managed venvs so
upgrades do not require replacing the desktop installation.

## The offline wheelhouse invariant

When `manifest.json` sets `"wheelhouse": {"complete": true}`, the Tauri runtime
installs the app wheel with `--no-index --find-links wheelhouse/` — it never
falls back to a pip index. An incomplete wheelhouse therefore means a hard
install failure (or, worse, a silently missing capability) on a customer's
first launch.

Two things must both hold, and BOTH are easy to get wrong:

1. Every requirement of `mclaw[desktop]` has a compatible wheel in
   `wheelhouse/` (right version, right platform/ABI tags).
2. `wheels/mclaw-*.whl` is **rebuilt** from the current `pyproject.toml`.
   Adding a dependency to `pyproject.toml` does not touch this wheel; a stale
   wheel omits the new `Requires-Dist` entirely, so the missing package never
   even enters the dependency graph — dropping the wheel file into
   `wheelhouse/` has no effect until the wheel is rebuilt.

`build/wheelhouse_check.py` checks both offline and exits non-zero on any gap
(MISSING / STALE-WHEEL / PLATFORM / VERSION). Run it after touching
dependencies or the wheelhouse, before packaging:

```bash
python build/wheelhouse_check.py                  # verify the shipped wheelhouse
python build/wheelhouse_check.py --download-missing  # fill gaps (needs network)
python build/wheelhouse_check.py --mark-complete     # only sets complete=true if it passes
```

If a package is only published as an sdist (e.g. `jieba`, the PyAutoGUI family),
`--download-missing` cannot produce a wheel from the index alone — convert it
with `pip wheel <pkg>` and drop the result into `wheelhouse/`. `uv` will not
build from source under `--no-index`. Remember to update `manifest.json`'s
`wheel.sha256` after rebuilding the Mclaw wheel.
