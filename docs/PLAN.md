# my-own-investor — Project Plan

**An AI-assisted, human-approved portfolio copilot for Interactive Brokers**, focused on
mid-horizon (months–years) growth investments in small/mid-cap **hardware for computing,
data centers, and connectivity**. It scans markets, whales, and prediction markets weekly;
runs ML models; produces a friendly dashboard report with buy/sell/hold suggestions; and
executes trades **only after explicit approval**.

Language: **Python**. Orchestration brain: **Claude (Agent SDK / CLI)** on a weekly schedule,
with urgent-alert interrupts.

> **Status (2026-07-11):** built through Phase 4 (dashboard + gated execution), plus
> post-plan additions: the `moi run` one-shot, holdings analytics (Portfolio +
> X-ray), and a Mission Control operations page (§9). See
> [IMPLEMENTATION.md](IMPLEMENTATION.md) for gate outcomes, including the Phase 2
> finding that a zero-parameter rank composite beat the LightGBM ranker out-of-sample.

---

## 1. Vision and guiding principles

Your original idea, sharpened into principles that will keep the project safe and useful:

1. **Copilot, not autopilot.** The system never trades on its own. Every action ends in a
   proposal card (ticker, action, size, thesis, confidence, risk) that you approve or reject.
   Approved orders are executed via the IBKR API; everything is journaled.
2. **Weekly cadence, urgent interrupts.** One deep pipeline run per week (weekend, after
   Friday close, before Monday open). A lightweight daily/intraday watcher only fires on
   defined triggers (earnings surprise, >X% move, whale filing, halted stock, thesis break).
3. **Signals are evidence, not orders.** Whale trades (Buffett/Ackman 13Fs, Pelosi/congress
   disclosures) arrive with 30–45 day lag — they are *features and thesis confirmation*, not
   copy-trade signals. Same for Polymarket probabilities: macro/event context, not entries.
4. **Mid-horizon discipline.** Position theses live for months–years. The ML layer ranks and
   sizes; it does not day-trade. Turnover is explicitly penalized.
5. **Paper first, then real.** Everything runs against an IBKR paper account until the
   backtest + 8–12 weeks of paper results justify going live.
6. **Everything explainable.** Every suggestion links to the features, news, filings, and
   model scores that produced it. If Claude can't explain it in three sentences, it doesn't
   ship in the report.

## 2. Investment universe

**Theme:** growth-driven, small-to-mid-cap, hardware-oriented — computing, data centers,
connectivity.

Sub-sectors and example names (illustrative, the screener builds the real list):

| Sub-sector | Examples |
|---|---|
| AI/compute silicon & IP | ALAB, CRDO, LSCC, SITM, penny-to-mid fabless |
| Optical & interconnect | COHR, LITE, AAOI, FN, APH (upper bound) |
| Data-center power & cooling | VRT, MOD, POWL, NVT, BE |
| Networking & connectivity | ANET (upper bound), CIEN, EXTR, CALX |
| Memory/storage/substrates | STX, WDC, smaller substrate/packaging plays |
| Test, fab equipment, materials | ONTO, ACMR, CAMT, UCTT, ICHR |

**Universe rules (screener, refreshed weekly):**
- US-listed (NYSE/Nasdaq), market cap **$300M – $30B** (configurable), ADV > $5M
- Sector/industry taxonomy match (GICS/SIC + keyword/embedding match on business description)
- Revenue growth or backlog growth signal present; exclude pre-revenue story stocks by default
- Output: ~80–150 tickers = the candidate pool for ranking and reports

## 3. System architecture

```
                        ┌─────────────────────────────────────────┐
                        │            Orchestrator                 │
                        │  Claude Agent SDK · weekly cron + alerts│
                        └──────┬──────────────┬───────────────────┘
                               │              │
         ┌─────────────────────┼──────────────┼──────────────────────┐
         ▼                     ▼              ▼                      ▼
 ┌──────────────┐      ┌──────────────┐  ┌──────────────┐   ┌──────────────┐
 │ Data Ingest  │      │ Feature Store│  │  ML Engine   │   │ Report/LLM   │
 │  (collectors)│ ───► │ DuckDB+      │─►│ rank · size  │──►│ thesis writer│
 │ prices,      │      │ Parquet      │  │ regime       │   │ suggestions  │
 │ filings,     │      └──────────────┘  └──────────────┘   └──────┬───────┘
 │ whales,      │                                                  │
 │ polymarket,  │      ┌──────────────┐  ┌──────────────┐          ▼
 │ news         │      │  Backtester  │  │ Risk Manager │   ┌──────────────┐
 └──────────────┘      │ walk-forward │  │ limits, stops│◄──│  Dashboard   │
                       └──────────────┘  └──────┬───────┘   │  (Streamlit) │
                                                │           └──────┬───────┘
                                                ▼                  │ approve
                                         ┌──────────────┐         ▼
                                         │  Executor    │◄── approval queue
                                         │ IBKR ib_async│    (paper → live)
                                         └──────────────┘
```

Seven Python packages in a monorepo (`src/moi/`): `ingest`, `features`, `ml`, `backtest`,
`risk`, `report`, `execute`, plus `dashboard/` and `orchestrator/`.

## 4. Data layer

### 4.1 Sources

| Domain | Source | Access | Notes |
|---|---|---|---|
| Prices, fundamentals | IBKR market data via [ib_async](https://github.com/ib-api-reloaded/ib_async); `yfinance` as free fallback | API | ib_async is the maintained successor to ib_insync |
| Portfolio & orders | IBKR TWS / IB Gateway | ib_async | Paper account first |
| SEC filings, 13F whales, insider Form 4 | [edgartools](https://github.com/dgunning/edgartools) on SEC EDGAR | Free API | Buffett (Berkshire), Ackman (Pershing Square), plus a configurable whale list; QoQ position diffs (NEW/INCREASED/…) built in |
| Congress trades (Pelosi et al.) | [Quiver Quantitative](https://www.quiverquant.com/congresstrading/) API or [Unusual Whales](https://unusualwhales.com/politics) API | Paid tier (~$10–75/mo) | STOCK Act disclosures, 30–45 day lag |
| Prediction markets | [Polymarket Gamma API](https://docs.polymarket.com/api-reference/introduction) (public, free) + CLOB price history | Free REST | Fed cuts, tariffs, elections, AI-regulation odds as macro features |
| News & sentiment | RSS/company PR + earnings-call transcripts; Claude summarizes/scores | Free/LLM | Sector-specific: hyperscaler capex news is the key macro driver for this universe |
| Macro | FRED (rates, PMI), hyperscaler capex from 10-Qs | Free | Capex trend = the theme's tide |

### 4.2 Storage

- **DuckDB + Parquet** (single-file analytics DB, zero ops, pandas/polars-native).
- Tables: `prices_daily`, `fundamentals_q`, `filings_13f`, `congress_trades`,
  `insider_form4`, `polymarket_series`, `news_items`, `features_weekly`,
  `signals`, `suggestions`, `orders`, `portfolio_snapshots`, `report_runs`.
- Every collector is idempotent (`run_id`, upsert by natural key) so weekly re-runs are safe.

## 5. Feature engineering

Weekly per-ticker feature vector (~60–120 features), all point-in-time correct:

- **Momentum/technical:** 4/13/26/52-week returns, distance from 52w high, volatility,
  volume trend, relative strength vs SMH/SOXX and vs the custom universe index.
- **Fundamentals:** revenue growth (TTM, QoQ acceleration), gross-margin trend, backlog/RPO
  growth where disclosed, EV/S vs growth (rule-of-40-ish score), dilution rate.
- **Whale features:** net institutional add/trim from 13F diffs (edgartools), number of
  tracked whales holding, congress-trade buy/sell counts (decayed by disclosure lag),
  insider Form 4 cluster buys.
- **Theme/macro features:** hyperscaler capex growth estimate, Polymarket-implied
  probabilities (rate cuts, tariff events, AI-related markets), semis cycle indicators
  (WSTS billings, SOX momentum), Polymarket↔sector rolling correlations.
- **Text features (Claude):** earnings-call tone score, guidance direction, "AI/data-center
  exposure" score extracted from the latest 10-K/10-Q business sections.

Correlation/trend mining (your "find global trends" item): a weekly job computes rolling
correlations between Polymarket series / macro series and universe returns, flags regime
changes (e.g., "rate-cut odds ↑ 20pts; small-cap hardware beta to this is +0.4"), and feeds
the strongest ones into both the ML model and the written report.

## 6. ML engine

State of the art for this problem size is **not** deep RL — it's disciplined
cross-sectional ranking. (FinRL-style RL and Qlib's exotic models overfit badly on a
~100-name universe with weekly bars; see §10.)

1. **Ranking model (core):** LightGBM/XGBoost cross-sectional ranker predicting 3-month
   forward relative return within the universe. Weekly scores → decile ranks.
   Purged, embargoed walk-forward CV (à la López de Prado) — no look-ahead, no leakage.
2. **Regime model:** small classifier (or rules) on macro/Polymarket/SOX features →
   risk-on / neutral / risk-off. Scales gross exposure and cash level.
3. **Portfolio construction:** rank scores + risk model → target weights via
   [skfolio](https://github.com/skfolio/skfolio) / PyPortfolioOpt-style mean-variance with
   constraints: max 8% per name, max 30% per sub-sector, 10–25 positions, turnover penalty,
   min holding period ~8 weeks.
4. **Backtesting:** vectorbt (fast research loop) + a slower event-driven check with
   realistic costs/slippage before any strategy change ships. Baselines it must beat:
   equal-weight universe, SMH, SPY.
5. **Conformal confidence:** each suggestion carries a calibrated confidence interval so
   the report can honestly say "weak signal" — suggestions below threshold are filtered.

Metrics tracked per run: IC / rank-IC, top-minus-bottom decile spread, Sharpe/Sortino,
max drawdown, turnover, hit rate — all visible on the dashboard's "Model health" page.

## 7. Claude orchestration (agents)

Built on the **Claude Agent SDK (Python)** with scheduled runs (cron / `claude` headless
mode). Multi-agent design inspired by [TradingAgents](https://github.com/TauricResearch/TradingAgents)
(arXiv:2412.20138) — specialized analysts + adversarial debate — but simplified:

| Agent | Job |
|---|---|
| **Scanner** | verifies data freshness, runs collectors, flags anomalies |
| **Quant analyst** | runs feature/ML pipeline, interprets scores, model-health check |
| **Fundamental analyst** | reads new filings/transcripts for top candidates & holdings, writes/updates thesis per position |
| **Whale watcher** | diffs 13F/congress/insider data, writes "whale moves" section |
| **Macro/trends analyst** | Polymarket + macro correlations, regime narrative |
| **Bear (red team)** | argues the *other* side of every proposed action: objections against buys, the give-up case against sells |
| **Risk officer** | grades every proposed action strong/moderate/weak by how many independent signals agree; the grade lands in the queue's `confidence` field |
| **Exit reviewer** | sell-side sweep of every *held* position — KEEP/WATCH/EXIT verdicts, so deteriorating holdings surface even when the diff proposes nothing |
| **Portfolio manager** | merges all inputs → final action list with sizes, rationale, confidence; queues for approval |

**Ordering contract:** every signal digest (holdings, whales/insiders, macro,
prediction markets, news) is assembled into one bundle *before* any agent runs, and
every agent receives that full bundle with the proposed actions listed last — so
theses, bear cases, confidence grades, and exit verdicts are always judgments over
the complete picture, never over a single feed.

**Weekly run (Sat 09:00):** collect → features → ML → signal bundle → agents →
report → dashboard + notification. **Urgent watcher (daily 17:30 ET, ~2 min):** checks triggers — holding moves
>±12% in a day, earnings surprise, new whale filing touching a holding, stop/thesis-break
level hit, Polymarket event probability jumps >20pts — and if fired, runs a mini-report and
pushes an alert.

## 8. Execution with approval (human-in-the-loop)

1. PM agent writes proposed orders to the `suggestions` table with status `PENDING`.
2. Dashboard "Approval queue" shows each card: action, size, limit price, thesis,
   bear-case, confidence, portfolio impact. Buttons: **Approve / Edit / Reject / Snooze**.
3. On approval, the executor places the order via ib_async (default: limit orders, GTC,
   with a max-slippage guard), monitors fills, and journals everything.
4. Hard safety rails in code (not in the LLM): whitelist of tradable tickers (the universe),
   max order value, max daily total, no shorting/derivatives in v1, kill switch, and the
   executor refuses anything not present in an approved suggestion row.

## 9. Dashboard

**Streamlit** (pure Python, fastest to build; migrate to FastAPI+React later only if
needed), split into `app.py` (navigation), `mission.py`, `views.py`, and `common.py`,
with the operational logic in `src/moi/ops.py` so it stays unit-testable. Pages are
grouped into three sections:

**Operate**
1. **Mission control** (landing page) — health strip (report age, pending queue,
   snapshot age, source freshness); one-click commands (collect, report, full
   `moi run`, urgent triggers, fill sync, IBKR ping, model eval) that run as
   detached `python -m moi` subprocesses with a live log tail; a connections board
   (broker, EDGAR, FRED, congress, agents, scheduler, trading mode) with green/red
   per dependency; data-source freshness merged with each collector's last run;
   recent run history
2. **Weekly report** — this week's narrative, action cards, and holdings exit
   review; downloadable as markdown or self-contained phone-friendly HTML
3. **Approval queue** — pending suggestions with one-click approve/reject/snooze
   and the approximate dollar size of each trade

**My money**
4. **Portfolio** — holdings with P&L vs cost, trailing returns (1W–1Y) vs SPY,
   indexed relative-performance charts, weights
5. **Holdings X-ray** — frozen-weights book behavior: growth vs SPY/QQQ/SMH,
   beta/vol/Sharpe/drawdown per holding, return contribution, correlation heatmap,
   computed plain-language insights (concentration, lockstep pairs, dead weight)
6. **Journal** — every suggestion, decision, and order ever made (auditability)

**Research**
7. **Candidates** — ranked universe table with scorer inputs, held-ticker markers
8. **Whales** — tracked investors' latest moves and overlap with your book
9. **Trends** — Polymarket probabilities, FRED macro series
10. **Model health** — composite-vs-challenger metrics, rank-IC history, backtests

Because DuckDB is single-writer, the dashboard never runs pipeline code in-process:
Mission control launches one background job at a time, and data pages degrade to a
"database busy" notice while a job holds the write lock.

The weekly report is written as markdown plus a self-contained HTML twin
(`src/moi/report/html.py`: responsive, dark-mode aware, no external assets — built
to read well on a phone) and delivered by email/Telegram.

## 10. Related work (what we borrow, what we avoid)

- **[TradingAgents](https://arxiv.org/abs/2412.20138)** (Tauric Research) — multi-agent LLM
  trading firm simulation (analysts, bull/bear researchers, risk team). *Borrow:* role
  specialization + adversarial bull/bear debate before decisions. *Avoid:* its daily-trading
  focus; we run weekly with a longer horizon.
- **[FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)** / AI4Finance — open
  LLM agent platform for equity research. *Borrow:* auditable, report-first agent outputs.
- **[Microsoft Qlib](https://github.com/microsoft/qlib)** — full AI-quant platform
  (LightGBM→transformers, RD-Agent factor mining). *Borrow:* pipeline discipline
  (point-in-time data, walk-forward, model zoo as reference). *Avoid:* adopting the whole
  platform — too heavy for a personal ~100-name weekly system.
- **[FinRL / FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading)** — deep RL
  trading. *Avoid* as core: RL on weekly small/mid-cap data is a known overfitting trap;
  keep as a later experiment behind the backtest gate.
- **[OpenBB](https://github.com/OpenBB-finance/OpenBB)** — open financial data platform.
  *Borrow:* optional unified data adapters; good fallback for fundamentals.
- **[edgartools](https://github.com/dgunning/edgartools)** — SEC EDGAR parsing incl. 13F
  QoQ diffs and Form 4. Core dependency for the whale layer.
- **[Quiver Quantitative](https://www.quiverquant.com/congresstrading/)** /
  [Unusual Whales](https://unusualwhales.com/politics) — congress-trade APIs. Academic
  literature on copying congress/13Fs shows modest, decaying alpha due to disclosure lag —
  hence "feature, not signal."
- **[Polymarket APIs](https://docs.polymarket.com/api-reference/introduction)** (Gamma/CLOB/
  Data, free) — event probabilities as macro features; research shows prediction markets are
  well-calibrated for macro events.
- **López de Prado, *Advances in Financial Machine Learning*** — purged CV, embargoing,
  deflated Sharpe; the backtesting hygiene bible this plan follows.
- **[ib_async](https://github.com/ib-api-reloaded/ib_async)** — maintained ib_insync
  successor; plus IBKR's official Web/Client-Portal API as an alternative.

## 11. Tech stack

| Layer | Choice |
|---|---|
| Language / packaging | Python 3.12, conda (`environment.yaml`), `ruff`, `pytest`, `pydantic` v2 |
| Data | DuckDB + Parquet, `polars`/`pandas` |
| Broker | ib_async + IB Gateway (paper → live), Docker-ized gateway |
| ML | LightGBM, scikit-learn, skfolio, vectorbt, SHAP |
| LLM/agents | Claude Agent SDK (Python), claude CLI headless for cron |
| Dashboard | Streamlit + Plotly |
| Scheduling | cron/launchd → orchestrator; Telegram/email alerts |
| Secrets | `.env` + keychain; never in repo |

## 12. Roadmap

Detailed task-level breakdown with acceptance gates: **[IMPLEMENTATION.md](IMPLEMENTATION.md)**.

**Phase 0 — Foundations (wk 1–2):** repo scaffold, config, DuckDB schema, IBKR paper account
+ ib_async connectivity, price collector, universe screener v1.
**Phase 1 — Data spine (wk 3–5):** edgartools 13F/Form-4 collectors + whale list, congress
API, Polymarket collector, news ingester; nightly cron; data-quality checks.
**Phase 2 — Features & ML (wk 6–9):** feature store, ranking model + purged walk-forward
backtest vs baselines, regime model, portfolio constructor. *Gate: backtest beats
equal-weight universe after costs.*
**Phase 3 — Agents & report (wk 10–12):** agent roles, weekly markdown report end-to-end,
urgent watcher.
**Phase 4 — Dashboard & approval loop (wk 13–15):** Streamlit app, approval queue, paper
execution with safety rails.
**Phase 5 — Paper-trade evaluation (wk 16–24):** run fully for 8–12 weeks; track live vs
backtest; tune. *Gate: no critical failures, tracking within tolerance.*
**Phase 6 — Go live (small):** real account at reduced size; scale gradually; keep paper
running in parallel as control.

## 13. Risk management & compliance

- Personal use, own account — no investment-advice distribution. Reports carry a
  "not financial advice / model output" banner.
- Position limits, sector caps, cash floor, max drawdown circuit breaker (system stops
  proposing buys at −15% portfolio drawdown until reviewed).
- Small/mid-cap specific: liquidity screen (position ≤ 1% of ADV), earnings-date awareness,
  wide-limit orders only.
- Whale/congress data used only from public disclosures (STOCK Act, EDGAR) — fully legal.
- Weekly automated "model drift + data staleness" check; the system degrades to
  "report-only mode" if data quality fails.

## 14. Repository layout

```
my-own-investor/
├── environment.yaml          # conda environment
├── docs/
│   ├── PLAN.md               # this file
│   └── IMPLEMENTATION.md     # phased build plan with acceptance gates
├── config/                   # universe rules, whale list, limits (YAML)
├── src/moi/
│   ├── ingest/               # collectors: ibkr, edgar, congress, polymarket, news, macro
│   ├── features/             # feature builders + store
│   ├── ml/                   # ranker, regime, portfolio, conformal
│   ├── backtest/             # walk-forward engine, baselines, metrics
│   ├── risk/                 # limits, stops, circuit breakers
│   ├── report/               # weekly report builder (md/html)
│   ├── execute/              # ib_async order layer + safety rails
│   └── orchestrator/         # agent definitions, weekly & urgent runs
├── dashboard/                # Streamlit app
├── data/                     # DuckDB + parquet (gitignored)
├── notebooks/                # research
└── tests/
```

---

**Next step:** Phase 0 of [IMPLEMENTATION.md](IMPLEMENTATION.md) — scaffold the repo,
define `config/universe.yaml` and `config/whales.yaml`, and get the first IBKR
paper-account connection working.
