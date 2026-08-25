# FTSE India Rebalancing Predictor

Predicts which stocks FTSE will add to and remove from its India index
(FTSE All-World India, part of the FTSE Global Equity Index Series) at
each quarterly review. **Fully automated** — no manual data refresh
required. Designed to keep running unattended indefinitely.

**Validated backtest performance** (2022-2026, against FLIN N-PORT
holdings as ground truth): ~95% recall on additions, ~38-58% same-review
hit rate depending on quarter type. Full evidence trail in
[`BACKTEST.md`](BACKTEST.md). **Removes are not statistically validated**
— only 4 genuine removal events exist in the whole ground-truth history.
Treat any removes output as a directional watchlist, not a call.

## The core finding this model is built on

March/September and June/December reviews are governed by **different
FTSE rules**, confirmed by a near-perfect split in the data: every
historical June/December addition was a company listed under 12 months
(median 5.5 months); March/September additions had a median listing age
of 193 months. So:

- **March / September** = general semi-annual review. Ranks the eligible
  universe by market cap, cumulates to a coverage threshold, applies
  liquidity and a minimum trading record.
- **June / December** = fast entry only. New listings above an absolute
  size floor. Applying a coverage percentile here — the natural first
  instinct — is measurably wrong; the two populations need different
  screens entirely.

## How the automation works

Two scheduled GitHub Actions workflows, no manual steps:

**`predict.yml`** runs three times per quarter (day 3, 10, 17 after each
cut-off — 31 Jan/30 Apr/31 Jul/31 Oct) as a safety net against source
data not being ready yet. Every run:
1. Refreshes daily volume (incremental — only new trading days, seconds not hours)
2. Refreshes NSE free float / shares / listing dates, but **only on the
   first run of the quarter** — shareholding filings are themselves
   quarterly, so refreshing this on every run would triple NSE's fragile
   scraping risk for no benefit
3. Predicts the upcoming review
4. Appends the result to `outputs/rebalancing_predictions.xlsx`
5. Commits the update

**`update_backtest.yml`** runs monthly, checking SEC EDGAR for a new
FLIN N-PORT filing. If one has appeared for a quarter that was
predicted, it scores that prediction (caught/missed/false positive)
directly into the same workbook, and rolls the constituent baseline
forward. If nothing new is found, it exits quietly — that's the normal
case most months.

### Data safety — every automated source has a fallback

NSE and yfinance both broke in real, specific ways during this
project's development (see `BACKTEST.md`): wrong field names returning
silent nulls, session/rate-limit failures, a genuine 10x share-count
error. Automating them unattended only works if a bad pull can never
silently corrupt the model. So every fetch goes through
`data_health.py` before being trusted:

- A new value is checked against the last known good value (out of
  range, implausible jump, mass-null response)
- If it fails, the **old value is kept** and the event is logged
- If enough failures happen in one run, a **red warning banner** gets
  written into the top of the Summary sheet in plain language — no
  jargon, designed to be understandable by someone with zero technical
  background: *"some data could not be refreshed this run and OLDER
  data was used instead."*

The system degrades gracefully and says so. It should never produce a
confidently wrong answer without flagging that something's off.

## The permanent output file

`outputs/rebalancing_predictions.xlsx` is the **single, ever-growing**
source of truth — seeded with real historical predictions and outcomes,
never regenerated from scratch. Every automated run appends new rows in
the exact same format as the existing history; when real N-PORT data
lands for a predicted quarter, the matching rows get their outcome
columns filled in automatically. Six sheets: `Summary`, `Lag Analysis`,
`Adds Detail`, `Removes Detail`, `Lag Detail`, `Notes`.

**Delivery:** the end user gets one permanent link, sent once, that
never changes:

```
https://raw.githubusercontent.com/<your-username>/ftse-india-predictor/main/outputs/rebalancing_predictions.xlsx
```

Every scheduled run updates the file this link points to. No email
automation, no login, nothing to expire.

## Required data files (`data/`)

Two files need to be uploaded once, manually, before automation starts
— see `data/README.md` for exactly what's needed. Everything else in
`data/` (`nse_cache.json`, `daily_volume_panel.csv`) is created and
maintained by the workflows themselves.


## Known, deliberate limitations

Read these before trusting a specific prediction — each was tested, not
assumed:

- **India-only ranking approximates a regional cutoff.** FTSE's real
  boundary runs across the whole Asia Pacific ex Japan ex China region;
  we can only rank India against itself. This is the single largest
  source of remaining error and cannot be fixed without building the
  full regional universe.
- **Uneven parameter validation.** `entry_coverage` (88%) was validated
  with an out-of-sample split-half test and held up. `exit_coverage`,
  the fast-entry size floor, and the fast-entry window were tuned on the
  full historical sample and should be treated as provisional — there
  are only 11 fast-entry events and 4 remove events in the entire
  ground truth.
- **Ranking uses full market cap, not investable market cap**, despite
  the published Ground Rules specifying IMC. Tested head-to-head on
  identical data: IMC cumulation scored dramatically worse (~73% vs
  ~95%+ recall), most likely because free-float data is too noisy to
  cumulate reliably. This is a data-quality substitution, not a
  methodology claim.
- **True cut-off pricing was tested and does not help.** Fresher price
  data at the exact cut-off date performed *worse* than the simpler
  quarter-end proxy in a controlled comparison — the boundary is dense
  enough that single-day price noise dominates over data staleness. The
  quarter-end proxy is kept deliberately, not as an unfixed shortcut.
- **Foreign ownership headroom was tested and does not separate
  predictions.** Checked directly against real Bloomberg data for both
  adds and removes; did not distinguish genuine outcomes from false
  ones in either case. Not included in the model.
- **Universe is Nifty 500**, not the full NSE mainboard.
- **Some names pass every implemented rule and are still never added**
  (most persistently: Fertilisers and Chemicals Travancore Ltd, a
  ~90%-government-owned PSU). This is evidence the public Ground Rules
  do not fully specify FTSE's actual decision process — not a bug.

## Repository structure

```
src/ftse_predictor/
    config.py       thresholds, one source of truth
    cutoffs.py       true cut-off dates, listing-age lookups
    liquidity.py     the liquidity test
    universe.py      ranking and constituent tagging
    predict.py       the dual-rule prediction engine
    report.py        Excel formatting
scripts/
    run_prediction.py     predict the next review
    update_backtest.py    ingest real holdings, score, roll baseline forward
data/                     source CSVs (see table above)
outputs/
    predictions/          one dated Excel per run
    pending_predictions.csv
    backtest_log.csv
BACKTEST.md               full evidence trail behind every threshold
```
