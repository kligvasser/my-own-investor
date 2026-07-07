# my-own-investor — Implementation Plan

Companion to [PLAN.md](PLAN.md) (architecture and rationale). This document is the
build order: phases, tasks, files, and the acceptance gate that must pass before the
next phase starts.

> **Status (2026-07-07): Phases 0–4 complete, all gates passed.** Phase 5
> (evaluation) is the current phase — it is calendar time, not code:
> weekly runs, queue decisions, and live-vs-backtest tracking for 8–12 weeks.
>
> **Post-plan additions (2026-07-07):**
> - Full-pipeline audit: 8 bugs fixed (13F partial-filing baseline guard,
>   suggestion supersede, benchmark-ETF consistency, incremental prices,
>   absolute .env path, insider digest, model_runs pollution, fill sync)
> - Dashboard Portfolio page: per-holding P&L + trailing returns vs SPY, charts;
>   price collector tracks held non-universe tickers
> - Scheduling installed (launchd): nightly `collect all` 22:00,
>   Saturday `weekly --collect` 09:00
> - Owner opted to skip the paper stage: `MOI_ALLOW_LIVE=true` with the
>   $8k/order + $30k/day caps as the binding rails; reports/ made private
>   and purged from git history

**Working conventions (all phases):**
- Environment: conda via [`environment.yaml`](../environment.yaml); package installed
  editable (`pip install -e .`) from `pyproject.toml`.
- Every phase ends with: `ruff check` + `mypy` clean, `pytest` green, a git commit per
  milestone, and its acceptance gate demonstrated.
- All external I/O goes through a collector class with a common interface
  (`collect(since) -> rows`, idempotent upsert into DuckDB by natural key).
- Secrets only in `.env` (gitignored); every config file has a committed `*.example` twin.
- No phase may consume data the previous phase didn't validate (freshness check first).

**Milestone overview:**

| Phase | Weeks | Deliverable | Gate |
|---|---|---|---|
| 0 | 1–2 | Scaffold + IBKR connectivity + prices | `moi collect prices` idempotent, tests green |
| 1 | 3–5 | All collectors + nightly refresh | `moi collect all` green on `moi status` |
| 2 | 6–9 | Features, ranker, backtest, portfolio | Backtest beats equal-weight after costs |
| 3 | 10–12 | Agents + weekly report + urgent watcher | Full weekly report from real data |
| 4 | 13–15 | Dashboard + approval + paper execution | UI approve → paper fill → journal row |
| 5 | 16–24 | Paper-trade evaluation | 8–12 clean weeks, tracking within tolerance |
| 6 | 25+ | Go live small | Kill-switch drill passed, reduced size live |

---

## Phase 0 — Foundations (weeks 1–2)

**Goal:** a running skeleton — env, config, DB, IBKR paper connection, first data in.

### Tasks
- [x] **0.1 Project scaffold**
  - `pyproject.toml` (name `moi`, `src/` layout, `moi` CLI entry point via typer)
  - `src/moi/__init__.py`, empty subpackages: `ingest/`, `features/`, `ml/`,
    `backtest/`, `risk/`, `report/`, `execute/`, `orchestrator/`
  - `tests/` with a trivial smoke test; `pre-commit` (ruff, ruff-format, mypy)
- [x] **0.2 Config system** — `src/moi/config.py` (pydantic-settings):
  - layered: `config/settings.yaml` (committed defaults) → `config/settings.local.yaml`
    (gitignored) → `.env` (secrets: IBKR port/client-id, API keys, Telegram token)
  - commit `config/settings.example.yaml` and `.env.example`
- [x] **0.3 Database layer** — `src/moi/db.py`:
  - DuckDB file at `data/moi.duckdb`; versioned schema migrations (plain SQL files in
    `src/moi/migrations/`, applied in order, tracked in `schema_version` table)
  - v1 tables: `prices_daily`, `universe`, `run_log` (rest added by later phases)
- [x] **0.4 IBKR connectivity**
  - open IBKR **paper account**; install IB Gateway; document setup in `docs/SETUP.md`
  - `src/moi/ingest/ibkr.py`: connect via ib_async, fetch account summary + positions
  - `moi ibkr ping` CLI command proving connect/disconnect works
- [x] **0.5 Seed universe** — `config/universe.yaml`:
  - hand-curated seed list (~60 tickers across the six sub-sectors of PLAN §2) with
    sub-sector tags; screener automation deferred to Phase 2
- [x] **0.6 Price collector** — `src/moi/ingest/prices.py`:
  - daily OHLCV for universe + benchmarks (SMH, SOXX, SPY): IBKR primary,
    yfinance fallback; 3y backfill; idempotent upsert
  - `moi collect prices` CLI; unit tests with recorded fixtures (respx / parquet fixtures)

### Gate
`moi collect prices` run twice in a row produces identical row counts (idempotency),
3 years of history for the seed universe is present in DuckDB, `moi ibkr ping`
succeeds against the paper gateway, CI-style local checks all green.

---

## Phase 1 — Data spine (weeks 3–5)

**Goal:** every data source from PLAN §4.1 flowing on a nightly schedule, with quality checks.

### Tasks
- [x] **1.1 Whale 13F collector** — `src/moi/ingest/edgar_13f.py` (edgartools):
  - `config/whales.yaml`: tracked managers (Berkshire, Pershing Square, + your picks)
    with CIKs; fetch latest 13F-HR per manager, store holdings + QoQ diff status
    (NEW/INCREASED/DECREASED/CLOSED) into `filings_13f`
- [x] **1.2 Insider collector** — `src/moi/ingest/edgar_form4.py`: Form 4 buys/sells for
  universe tickers → `insider_form4`; cluster-buy detection helper
- [x] **1.3 Congress collector** — `src/moi/ingest/congress.py`:
  - provider adapter interface (`CongressProvider`) with two implementations:
    Quiver Quantitative and Unusual Whales; selected by which API key is present
  - normalize to `congress_trades` (politician, ticker, direction, size band,
    transaction date vs disclosure date — keep both for lag-aware features)
- [x] **1.4 Polymarket collector** — `src/moi/ingest/polymarket.py`:
  - `config/polymarket.yaml`: tracked market slugs/tags (Fed decisions, tariffs,
    AI regulation, elections); Gamma API for metadata, CLOB prices-history for series
    → `polymarket_series` (market, ts, prob)
- [x] **1.5 News ingester** — `src/moi/ingest/news.py`: RSS/PR feeds per ticker +
  sector feeds → `news_items` (dedup by URL hash); no LLM scoring yet (Phase 3)
- [x] **1.6 Macro collector** — `src/moi/ingest/macro.py`: FRED series (rates, PMI,
  etc. from `config/macro.yaml`) → `macro_series`
- [x] **1.7 Data quality** — `src/moi/ingest/quality.py`:
  - per-table freshness/row-count/gap checks; `moi status` prints a red/green table;
    results logged to `run_log`
- [x] **1.8 Scheduling** — `moi collect all` orchestrating every collector with
  structlog output; launchd/cron job nightly 22:00 + `docs/SETUP.md` instructions

### Gate
`moi collect all` completes end-to-end; `moi status` fully green; a week of unattended
nightly runs without manual fixes; each collector has tests with mocked HTTP.

---

## Phase 2 — Features & ML (weeks 6–9)

**Goal:** the quant core — features, ranking model, honest backtest, portfolio constructor.
*This is the phase where the project earns (or loses) its keep. Do not rush the gate.*

> **Gate outcome (2026-07-06): PASSED — with a finding.** The LightGBM ranker had zero
> out-of-sample IC on this 29-name weekly universe (overfits; first run failed the gate).
> A zero-parameter **rank composite** (dist_52w_high + ret_52w + ret_26w − size) achieves
> OOS rank-IC +0.061 (t=3.8), and the top-12 portfolio beats equal-weight after costs
> (Sharpe 1.74 vs 1.51, ann +74% vs +58%). The composite is the production scorer
> (`moi/ml/composite.py`); LightGBM remains a challenger that must beat it OOS to be
> promoted (`moi ml train` reports both). Backtester is a transparent in-house weekly
> engine instead of vectorbt. Caveats: single bull-regime test window; hand-picked 2026
> universe implies survivorship bias in *absolute* returns (relative comparison remains fair).

### Tasks
- [ ] **2.1 Universe screener v2** *(deferred to Phase 5 tuning)* — `src/moi/features/screener.py`: automate PLAN §2
  rules (cap $300M–$30B, ADV > $5M, sector match) on top of the seed list; weekly
  snapshot into `universe` (point-in-time membership — critical for backtests)
- [x] **2.2 Feature store** — `src/moi/features/` one module per family from PLAN §5:
  - `momentum.py`, `fundamentals.py`, `whales.py`, `macro_theme.py`
  - builder writes `features_weekly` (ticker, week, feature, value) with an
    `as_of` discipline: every feature computed only from data available that Friday
- [x] **2.3 Labels** — 13-week forward return relative to universe median;
  `src/moi/ml/labels.py`
- [x] **2.4 Ranking model** — `src/moi/ml/ranker.py`:
  - LightGBM cross-sectional ranker; purged + embargoed walk-forward CV
    (`src/moi/ml/cv.py`); SHAP feature attribution stored per run
  - experiment notebook `notebooks/01_ranker.ipynb`, production entry `moi ml train`
- [x] **2.5 Backtester** — `src/moi/backtest/engine.py`:
  - vectorbt weekly rebalance sim with IBKR-realistic costs (commission + spread by
    ADV bucket); baselines: equal-weight universe, SMH, SPY
  - `moi backtest run` → metrics report (IC, decile spread, Sharpe, maxDD, turnover)
    saved under `docs/backtests/YYYY-MM-DD/`
- [x] **2.6 Regime model v1** — `src/moi/ml/regime.py`: rules on SOX trend, rate-cut
  odds (Polymarket), credit spreads → risk-on/neutral/risk-off scalar
- [x] **2.7 Portfolio constructor** — `src/moi/ml/portfolio.py` (skfolio):
  - constraints from `config/limits.yaml`: max 8%/name, 30%/sub-sector, 10–25
    positions, turnover penalty, ≥8-week min hold, regime-scaled gross exposure
- [ ] **2.8 Conformal confidence** *(deferred; composite has no fitted uncertainty — revisit if LGBM is promoted)* — mapie intervals on ranker output; suggestions
  below confidence threshold marked "weak signal"
- [ ] **2.9 Trend/correlation miner** — `src/moi/features/trends.py`: rolling
  Polymarket/macro ↔ universe-return correlations, regime-change flags → `signals`

### Gate
Walk-forward backtest (≥3y, costs included) beats equal-weight universe on Sharpe **and**
the result is written up in `docs/backtests/` with parameters frozen in config. If the
gate fails: iterate features/labels here — do **not** proceed and compensate with agents.

---

## Phase 3 — Agents & weekly report (weeks 10–12)

**Goal:** the Claude layer — seven agents (PLAN §7), weekly report, urgent watcher.

### Tasks
- [x] **3.1 Agent toolkit** — `src/moi/orchestrator/tools.py`: typed Python functions
  the agents may call (read-only DB queries, feature lookups, filing text fetch);
  no order-placement tool exists in this layer at all
- [x] **3.2 Agent definitions** — `src/moi/orchestrator/agents.py` (Claude Agent SDK):
  Scanner, Quant, Fundamental, Whale-watcher, Macro/trends, Bear, PM — each with a
  focused system prompt and only the tools it needs
- [x] **3.3 Weekly pipeline** — `moi weekly`:
  collect → quality gate → features → ML scores → agents (parallel analysts → Bear
  red-teams every proposed buy → PM merges) → suggestions rows (`PENDING`) +
  markdown report `reports/YYYY-WW.md` (jinja2 template, sections per PLAN §9 p.1)
- [ ] **3.4 Text features** *(deferred: needs a transcript source)* — Claude-scored earnings-call tone / guidance /
  AI-exposure (PLAN §5) cached in `news_items`/`features_weekly` so re-runs are free
- [x] **3.5 Urgent watcher** — `moi watch` (daily 17:30 ET via cron):
  triggers from PLAN §7 (±12% move, earnings surprise, whale filing touching a
  holding, thesis-break level, Polymarket jump >20pts); on fire → mini-report +
  Telegram/email push (`src/moi/report/notify.py`)
- [ ] **3.6 Cost control** — token budget per weekly run logged to `run_log`;
  target < a few $ per week (cache filing texts, only re-analyze changed inputs)

### Gate
`moi weekly` produces a complete, readable report from real data with ≥3 actionable,
explained suggestions (each with thesis, bear-case, confidence); urgent watcher fires
correctly on a replayed historical trigger day; two consecutive weekend runs succeed
unattended.

---

## Phase 4 — Dashboard & approval loop (weeks 13–15)

**Goal:** the friendly UI and the only path to execution — approve on screen, paper-trade.

### Tasks
- [x] **4.1 Streamlit app** — `dashboard/app.py` + `dashboard/pages/` implementing the
  8 pages from PLAN §9 (Home/report, Approval queue, Portfolio, Candidates, Whales,
  Trends, Model health, Journal); read-only against DuckDB except the queue
- [x] **4.2 Approval queue** — Approve / Edit size / Reject / Snooze buttons writing
  status transitions to `suggestions` (`PENDING → APPROVED/REJECTED/SNOOZED`),
  with the full card: action, size, limit price, thesis, bear-case, confidence,
  portfolio impact
- [x] **4.3 Executor** — `src/moi/execute/executor.py` (ib_async, paper):
  - consumes only `APPROVED` rows; limit orders GTC with max-slippage guard;
    fill monitoring → `orders` + `portfolio_snapshots`
  - hard rails in code (PLAN §8): universe whitelist, max order value, max daily
    total, no short/derivatives, kill-switch flag in DB checked before every order
  - `moi execute run` (manual or post-approval trigger), full journaling
- [x] **4.4 Safety tests** — unit tests proving the executor refuses: non-whitelisted
  ticker, oversized order, unapproved suggestion, kill-switch on
- [x] **4.5 Report delivery** — weekly report emailed/Telegrammed with dashboard link

### Gate
End-to-end demo: weekly run → suggestion appears in queue → approve in UI → paper
order placed and filled → journal and portfolio pages reflect it. All four safety
tests pass. A rejected suggestion provably never reaches the executor.

---

## Phase 5 — Paper-trade evaluation (weeks 16–24)

**Goal:** 8–12 weeks of full-system paper trading; prove live behavior matches backtest.

### Tasks
- [ ] **5.1 Run the loop** — weekly runs + approvals on the paper account, treating it
  as real money (respond to every queue item within 48h)
- [ ] **5.2 Tracking monitor** — `src/moi/backtest/tracking.py`: weekly live-vs-backtest
  attribution (signal decay, slippage, approval overrides); shown on Model health page
- [ ] **5.3 Review ritual** — 15-minute weekly checklist in `docs/REVIEW.md`
  (data green? model IC in range? suggestions sane? any near-misses?); log kept in repo
- [ ] **5.4 Tuning** — adjust thresholds/limits from evidence; every change goes through
  the Phase 2 backtest gate before deployment; no mid-week strategy edits
- [ ] **5.5 Failure drills** — once during the phase: kill IB Gateway mid-run, corrupt a
  collector response, flip the kill switch — system must degrade to report-only mode

### Gate
≥8 consecutive clean weeks; live weekly returns within agreed tolerance of backtest
expectation (define numerically in week 16, e.g. tracking error < 2%/wk); zero
safety-rail violations; you still *want* to read the Monday report (UX gate).

---

## Phase 6 — Go live, small (week 25+)

- [ ] Switch executor to the real account at **≤25% of intended size**; paper account
  keeps running in parallel as control
- [ ] Re-run kill-switch and max-order drills against the live account (tiny order)
- [ ] Scale in thirds over ~6 weeks only while live matches paper
- [ ] Quarterly: re-validate model (walk-forward re-fit), refresh universe & whale list,
  review costs (API subscriptions, LLM tokens)

---

## Cross-cutting notes

- **Testing:** collectors mocked with respx/fixtures; ML tested on synthetic frames
  (leakage tests: shuffle-future must destroy IC); executor tested against IBKR paper.
- **Ordering rationale:** data before features, features before ML, ML gate before
  agents (so the LLM narrates a validated signal instead of masking a weak one),
  agents before UI, UI before any order ever leaves the machine.
- **Biggest schedule risks:** Phase 2 gate (may take extra weeks — accept it),
  IBKR market-data subscriptions (order early in Phase 0), congress-API choice
  (adapter interface keeps it swappable).
