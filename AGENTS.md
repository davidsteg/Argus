# AGENTS.md — Rules for AI Agents Working on Argus

Argus is an async paper-trading bot on Alpaca (`alpaca-py`) with a NiceGUI
dashboard, running as two Docker containers sharing a SQLite volume. Read
this before touching anything — it encodes hard invariants, the actual
release pipeline, and mistakes already made and fixed once.

## Hard safety invariants — never weaken these

- **`paper=True` is hardcoded** in `backend/bot.py` (`TradingClient(...,
  paper=True)`) and in the frontend's emergency-stop client. Never make
  this configurable, env-driven, or conditionally live. If live trading
  is ever genuinely wanted, that is a deliberate, reviewed, standalone
  change — not a side effect of something else.
- **Every entry is a bracket order** (take-profit + stop-loss attached at
  submission). Never submit a bare market/limit buy with no exit attached.
- **The daily kill-sequence and dashboard EMERGENCY HARD STOP must always
  work independently of engine state** — `EngineController.kill()` and
  the frontend's `execute_hard_stop()` both construct their own
  `TradingClient` rather than depending on a running bot instance. Don't
  refactor this into a single path that could fail silently if the
  engine has crashed.
- Never remove or bypass `DAILY_STOP_LOSS`, `MAX_POSITIONS`, or
  `MIN_PRICE_USD` checks to "test something quickly."

## Architecture map

```
backend/
  bot.py          async engine: fetch bars → evaluate_signal → place_bracket_buy
                  → sync_portfolio/reconcile trades; run_cycle() is the
                  single per-minute state machine, EngineController owns
                  start/kill/reset lifecycle
  api.py          FastAPI debug/ops app (port 8000/8002 depending on deploy)
                  — THE primary diagnostic surface, see below
  optimizer.py    nightly grid search, midnight Europe/Zurich
  indicators.py   RSI / ATR / VWAP / bracket-distance math — SHARED by
                  bot.py and optimizer.py so live signals and backtests
                  can never drift apart. Any indicator change goes here,
                  never duplicated into either caller.
  regime.py       SPY trend + realized-vol classifier (RISK_ON/CAUTION/
                  RISK_OFF); gates new entries only, never forces exits
  sentiment.py    Alpaca news → Claude scorer → keyword heuristic →
                  neutral fallback, cached per symbol
  universe.py     static symbol list or dynamic most-actives watchlist
frontend/app.py   NiceGUI dashboard, reads the shared DB directly (no HTTP
                  hop to backend), independent Alpaca client for hard-stop
shared/
  database.py     thread-safe SQLite (WAL); DEFAULT_CONFIG is the single
                  source of truth for which bot_config keys are valid —
                  get_config() filters out anything else (see "Retired
                  config keys" below)
  version.py      __version__ + RELEASES list — dashboard and /version
                  both read this; keep in lockstep with CHANGELOG.md
```

## Strategy parameters live in the database, not env vars

`rsi_period`, `rsi_buy_signal`, `news_cutoff`, `atr_stop_mult`,
`atr_target_mult` live in the `bot_config` SQLite table and are rewritten
nightly by `optimizer.run_optimization()`. Env vars (`POSITION_SIZE_USD`,
`RISK_PER_TRADE_USD`, `MAX_POSITIONS`, `COOLDOWN_MINUTES`, etc.) are
operational knobs, not strategy parameters — don't conflate the two.

**When adding a new tunable strategy parameter**: add it to
`DEFAULT_CONFIG` in `shared/database.py`, to `PARAMETER_GRID` in
`optimizer.py`, thread it through `backtest()`/`indicators.py`, and use it
in `bot.py`'s `evaluate_signal`/`place_bracket_buy`. If you retire one,
remove it from `DEFAULT_CONFIG` — `get_config()` already filters any
lingering DB row for a key not in `DEFAULT_CONFIG`, so retiring is safe
without a migration.

**The optimizer must stay out-of-sample validated.** Since v2.2.0 it
splits history 75% train / 25% validation (`split_history`) and only lets
a parameter combination go live if it's also profitable on the unseen
validation window (see `run_optimization`'s ranking-then-validation-gate
loop). Never revert to picking the best in-sample combination outright —
that's how a 30-day grid search convinces itself a coin flip is a
strategy.

## The debug API is the primary diagnostic tool — use it before guessing

Before assuming code is broken, hit the running instance:
- `GET /health` — process/DB/Alpaca connectivity
- `GET /debug` — last cycle trace, active cooldowns, engine env
- `GET /signals` — full dry-run of the decision pipeline per symbol (regime →
  RSI → cooldown → VWAP → sentiment), the fastest way to answer "why isn't
  it trading"
- `GET /regime` — current SPY-based regime classification
- `GET /logs?limit=N` — this is what actually caught the v2.2.4 bracket-
  rejection bug: it looked like a one-off race the first time, and was
  only confirmed as a real recurring bug by reading several consecutive
  cycles of `/logs`, not by re-deriving from source. When something looks
  wrong, pull the logs across multiple cycles before theorizing.
- `POST /optimize` — trigger the grid search immediately instead of
  waiting for midnight, useful after any optimizer-relevant change
- Different deployments may run different versions — always check
  `GET /version` before diagnosing, don't assume localhost and a remote
  deploy match.

## Release pipeline — do not hand-roll an alternative

1. Bump `__version__` in `shared/version.py` **and** add a matching
   `## [vX.Y.Z]` section to the top of `CHANGELOG.md` (same version
   string, brackets included — the release workflow greps this exact
   format to extract release notes). Also add a matching entry to the
   `RELEASES` list in `version.py` (dashboard release-notes dialog reads
   this, not the changelog file).
2. Commit, then `git tag -a vX.Y.Z -m "..."`, then
   `git push origin master --follow-tags`.
3. Pushing a `vX.Y.Z` tag triggers
   `.github/workflows/docker-publish.yml`: builds and pushes
   `davidsteg/argus-backend` and `davidsteg/argus-frontend` to Docker Hub
   (tagged `X.Y.Z` + `latest`), then auto-creates a GitHub Release whose
   body is the CHANGELOG section extracted in step 1. This only fires on
   real version tags — never push an ad-hoc tag to test it, it will
   publish to Docker Hub for real.
4. Tag format is `vMAJOR.MINOR.PATCH` (the "v" prefix is mandatory — the
   workflow's tag filter and the changelog-extraction step both depend on
   it matching `v[0-9]+.[0-9]+.[0-9]+`).
5. `docker-compose.yml` **pulls from Docker Hub** (`ARGUS_VERSION` env var
   pins a version, defaults to `latest`) — this is the production/deploy
   file. `docker-compose.dev.yml` is a compose *override* for building
   from local source (`docker compose -f docker-compose.yml -f
   docker-compose.dev.yml up -d --build`). Don't merge these back into
   one file; the split is intentional so a plain `docker compose up -d`
   never silently rebuilds stale local code instead of pulling the
   release.
6. After a release, remember running deployments don't self-update —
   `docker compose pull && docker compose up -d` (or the dev-override
   build) is a separate, deliberate step on each host.

## Secrets

- `.env` is git-ignored (`.gitignore`) — never re-add it, never commit
  real keys. `.env.example` documents every variable with a placeholder
  and must stay current when env vars change.
- `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` live only as GitHub Actions
  repo secrets on this repo — secrets are per-repo and cannot be
  referenced across repos, so don't assume a token used in one project
  is available in another.
- If you ever see a real-looking API key/token in a chat message, a diff,
  or a file about to be committed, stop and flag it — don't silently
  commit or print it, even partially masked.

## Testing changes locally (Windows dev machine)

There is no system Python with `pandas`/`numpy`/`alpaca-py` preinstalled.
`py -m py_compile <file>` is a fast syntax check but doesn't catch logic
errors. For real verification, create a throwaway venv in the scratch
directory (`py -m venv <scratch>/venv`, then
`<scratch>/venv/Scripts/python.exe -m pip install pandas numpy alpaca-py
python-dotenv fastapi uvicorn`) and write a small standalone test script
that imports the modules under test — this caught real bugs (RSI/ATR/VWAP
edge cases, the train/validation split, config-key filtering) before
anything shipped. Don't skip this for changes to `indicators.py`,
`optimizer.py`, or `bot.py`'s signal/order logic.

## Known pitfalls (already hit once — don't reintroduce)

- **Bracket orders priced off stale bar data get rejected.** Fixed in
  v2.2.4: `place_bracket_buy` re-fetches the latest trade immediately
  before submission because the 1-minute bar close can be tens of
  seconds stale, and a tight ATR-scaled stop on a cheap/quiet symbol is
  often narrower than that drift. Don't reintroduce bracket math based
  purely on the bar snapshot.
- **`news_cutoff` at 0.55 silently blocks every symbol with no news**
  (neutral score defaults to 0.50, which is `<= 0.55`). This is currently
  tuned to 0.45 live in the DB so "no news" passes but bearish news still
  blocks — don't "fix" this back to 0.55+ without understanding it will
  mute most of a leveraged-ETF-heavy watchlist.
- **Retired config keys must be filtered, not migrated.** When
  `stop_loss_pct`/`take_profit_pct` were replaced by ATR multiples,
  `get_config()` was changed to only return keys present in
  `DEFAULT_CONFIG` — this is the pattern to repeat, not a SQL migration.

## Versioning & Releases

- Maintain `CHANGELOG.md` with version history and release notes.
- Every release commit gets a git tag: `vMAJOR.MINOR.PATCH`.
- Update `CHANGELOG.md` and `shared/version.py` together, before tagging.

## Code Quality

- No spaghetti code: clean, readable, well-structured code only.
- Fix issues properly — no dirty workarounds or temporary hacks.
- Single responsibility per module/function.
- Keep shared code truly shared — `indicators.py` exists specifically so
  live and backtest math cannot duplicate-and-drift.

## Documentation

- `README.md` must always describe the system as it currently exists —
  it drifted badly once (still described a retired Streamlit prototype
  through v2.1.0) and was rewritten in v2.2.0; don't let that happen again.
- Keep `CHANGELOG.md` and `shared/version.py` `RELEASES` in sync on every
  release, not just occasionally.
- Update `README.md`'s endpoint table if `api.py` endpoints change.

## Git Workflow

- Commit often with clear, descriptive messages explaining *why*.
- Tag every release; never force-push tags or rewrite published history
  without explicit user instruction.
- Run `git status` before any command that could discard uncommitted
  work, and check `git diff`/`git status` output for anything sensitive
  before staging broadly.
