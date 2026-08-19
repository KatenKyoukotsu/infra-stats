# Project instructions

## Quick reference

```bash
# Run all tests
.venv/bin/python -m pytest tests/ -v

# Run a single test file
.venv/bin/python -m pytest tests/test_analyzer.py -v

# Run a single test class/method
.venv/bin/python -m pytest tests/test_analyzer.py::RoundValueTest::test_round_half_up -v

# Syntax check all modules (fast, no deps)
.venv/bin/python -m py_compile app/main.py

# Lint/typecheck — NOT configured. If adding, prefer ruff + mypy.
```

## Architecture

FastAPI service that collects infrastructure metrics from VictoriaMetrics, analyzes them, and sends reports to a messenger (Clouds/BotX).

**Entry point:** `app/main.py` → `create_app()` → `app = create_app()`

**Core modules:**
- `app/config/` — YAML config loading with env overrides (secrets via `BOTX_BEARER_TOKEN`, `BOTX_CHAT_ID`, `API_KEY`)
- `app/vmclient/` — polite VictoriaMetrics client (semaphore + token bucket + retry)
- `app/analyzer/` — metrics analysis engine with TTLCache; `container.py` and `blackbox.py` are sub-modules
- `app/scheduler/` — APScheduler cron jobs (analyze + send)
- `app/storage/` — SQLite with WAL mode; `_last` report held in memory for diffs
- `app/notifier/` — BotX/Clouds messenger client
- `app/handlers/` — REST API endpoints

**Config priority:** ENV > `config.yaml` > defaults (see `app/config/config.py` for all defaults)

**Critical secret:** `bearer_token` must come from env (`BOTX_BEARER_TOKEN`), never committed in YAML.

## Conventions

- **Language:** Russian for docstrings, comments, and config descriptions. English for code identifiers.
- **Style:** No linting configured. Follow existing patterns — dataclasses, `from __future__ import annotations`, type hints on public APIs.
- **Tests:** `unittest` based, discovered by pytest. Use `unittest.mock` and `httpx.MockTransport` for HTTP mocking. Temp files via `tempfile`.
- **Config parsing:** Zero-value semantics (0/None/"" → default), matching Go conventions. See `_num()`, `_int()`, `_duration()`.
- **Query building:** PromQL queries use regex-escaped instance selectors. No subqueries (`:2m` rollups) — use `avg_over_time(...[period])` ratio approach.

## Working rules

1. **Plan before code:** For tasks >2 steps, write a step-by-step plan first. Don't code without user OK.
2. **Stop if stuck:** Don't pile up hacks. Rethink the approach.
3. **Autonomous debug:** Fix root causes from error logs. Don't ask to be led.
4. **Prove it works:** Never say "done" until you've logically verified correctness.
5. **Minimal changes:** Surgical edits only. Don't touch unrelated code.

## Gotchas

- SQLite calls in `Storage` use `run_in_executor` for async safety — keep both sync and async methods.
- `Engine._cache` is a `TTLCache` (not a dict) — don't replace with plain dict.
- `VmClient` has httpx `Limits(max_connections=max_concurrent)` — don't remove.
- `container.py` `group_labels` is configurable via `ContainersConfig.group_labels` — not hardcoded.
- Config file is mounted `:ro` in docker-compose — the app never writes to it at runtime (use `Manager.save()` + `apply_env_overrides()` for hot-reload).
- `.opencode/` and `.ua/` are gitignored. Plugin: `.opencode/understand-anything/`. Temp tests: `./tmp` (add to `.gitignore`).
