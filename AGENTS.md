# PinPilot — Codex project guide

## Project layout and architecture

- The FastAPI application starts in `app/main.py`. It registers the Etsy, Pinterest, creatives, experiments, and pipeline routers, serves the dashboard template and static assets, and initializes ORM metadata plus the additive analytics schema upgrade.
- SQLAlchemy engine/session setup is in `app/database.py`; settings and environment-backed configuration are in `app/config.py`. ORM entities are defined in `app/models/core.py` and exported through `app/models/__init__.py`.
- Business logic lives in `app/services/`. API routes are in `app/routers/`; scheduled entry-point scripts are in `scripts/`; the dashboard is `templates/dashboard.html` with styles in `static/style.css`.
- Schema evolution currently uses `Base.metadata.create_all` plus the guarded, additive `upgrade_analytics_schema` in `app/analytics_migrations.py`. It is not an Alembic migration setup. Preserve existing rows and constraints when extending this path; do not introduce a second migration system without an explicit architectural reason.
- The dashboard is server-rendered from the existing template and uses the current app routes/API. Keep UI-only changes in the template/static layer whenever possible.

## Existing Pinterest architecture

- Pinterest account, board, OAuth state, and encrypted OAuth credential handling are in `app/models/core.py` and `app/services/pinterest.py`; the HTTP/OAuth routes are in `app/routers/pinterest.py`.
- `app/services/pinterest_api.py` is the Pinterest API v5 HTTP adapter. `PinterestApiService` in `app/services/pinterest.py` provides the account/token-aware service layer. Keep HTTP details and provider response mapping behind these existing boundaries. Use the configured API base URL for supported production or Sandbox environments; do not invent endpoints, scopes, or payload fields.
- `PublishedPinterestPin` represents a confirmed external Pinterest Pin, while `PinterestPublishIntent` records the durable local publish attempt. `PinterestPublisher` in `app/services/pinterest_publisher.py` coordinates the provider and database records. A disabled provider is the safe default; `pinterest_publish_enabled` defaults to `False` in `app/config.py`.
- Do not enable real Pinterest publishing unless the user explicitly asks for activation. Treat timeouts, invalid responses, and other ambiguous outcomes as unknown and reconcile before retrying; do not blindly create a possible duplicate Pin. Keep mock/test providers separate from real HTTP providers.
- OAuth credentials and tokens must remain server-side and encrypted using the existing credential/token service and configured encryption key. Request only scopes needed by the implemented features; never log or expose tokens, client secrets, or other credentials.
- Analytics has two provider boundaries: the provider-neutral protocol/DTOs, normalizer, and `AnalyticsCollector` are in `app/services/pinterest_analytics.py`; `app/services/pinterest_analytics_provider.py` adapts the existing Pinterest API service to those DTOs. The collector persists pin/account snapshots and collection runs through the existing models. The API adapter exists, but do not assume it is automatically scheduled or that production analytics collection is active; inspect wiring before changing behavior.
- Analytics dashboard aggregation is in `app/services/analytics_dashboard.py`. Keep account-level and Pin-level metrics separate, preserve `NULL` as “not measured” rather than zero, and use publication-time metadata snapshots for historical analysis.

## AI, scheduling, workers, and tests

- `app/services/daily_pin_scheduler.py` prepares local scheduled Pins; it does not itself publish to Pinterest. `app/services/ai_pipeline.py` owns persistent pipeline state and centralized daily AI quota controls. `app/services/ai_worker.py` claims and processes queued generation jobs.
- Scheduled entry points are `scripts/generate_daily_pins.py`, `scripts/run_ai_worker.py`, and `scripts/sync_etsy_listings.py`. Their current systemd units/timers are under `deploy/systemd/` (`pinpilot-ai-worker`, `pinpilot-daily-queue`, and `pinpilot-etsy-sync`). Check the unit files before changing invocation or timing assumptions.
- Tests are under `tests/`. `tests/conftest.py` overrides the database with a temporary SQLite file, configures AI providers as mocks, creates an isolated schema, and applies the analytics schema upgrade. Keep tests isolated; do not use `.env` credentials or make real Pinterest, Etsy, Gemini, or OpenAI requests by default. Prefer injected fake providers or mocked HTTP transports.

## Working rules

- Before editing, inspect the relevant implementation, tests, and current Git status. Search for existing models/services before adding another abstraction or duplicate implementation.
- Prefer small, focused, backward-compatible changes that fit the existing architecture. Do not refactor unrelated code or change scheduler, worker, publishing, or analytics behavior outside the requested scope.
- Add or update focused tests for changed behavior. Run the relevant tests and, where practical, the full suite; run `git diff --check`. Report what was run and any failures accurately.
- Preserve pre-existing working-tree changes and untracked user files. Never use destructive Git operations to make the tree clean.
- Never display, modify, stage, or commit `.env` credentials unless the user explicitly requests a narrowly scoped safe change. Do not expose secrets in output, logs, exceptions, tests, or snapshots.
- Do not directly modify a production database, delete or alter backups, or alter `cloudflared-windows-amd64.exe`. Do not run destructive migrations or data-deleting operations; stop and obtain explicit approval first. Production data must be preserved.
- Do not commit, push, or deploy unless the user explicitly requests it. For a requested deployment, inspect repository/remote/VPS state first, run the relevant tests, follow the repository's existing deployment procedure, preserve `.env`, databases, backups, and untracked files, then verify services and relevant endpoints.

## Verified project status

- The repository contains the Pinterest API v5 adapter, OAuth/account service, publish intent/coordinator, and analytics API adapter described above.
- Real Pinterest publishing is guarded by an explicit feature setting that defaults to disabled. Do not treat API approval, production publishing activation, or successful live publishing as established without checking current evidence.
- The analytics collector and API adapter exist, but production collection scheduling/activation is not established by the code inspected here. Do not describe production analytics collection as active without verifying the current wiring and runtime configuration.
- Re-check the current branch, commit, working tree, settings, and deployment state for each task; this file is guidance, not a substitute for inspecting the live repository or production environment.
