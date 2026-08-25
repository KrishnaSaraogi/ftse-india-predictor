# Backtest evidence trail

Every threshold in `config.py` and every design choice in `predict.py`
traces back to a specific test recorded here. If you're tempted to
"improve" something, check whether it was already tried.

Ground truth throughout: FLIN (Franklin FTSE India ETF) quarterly
holdings, reconstructed from SEC N-PORT filings 2019–2026, manually
labelled to strip out corporate-action artifacts (mergers, splits,
demergers) from genuine index decisions. Confirmed against FTSE's own
published constituent file that FLIN fully **replicates** the index
(not samples it) — 272 holdings vs 272 published constituents, at the
one date both were available.

## Headline result

| | Recall | Hit rate |
|---|---|---|
| March/September (general review) | ~95% | 58–60% |
| June/December (fast entry) | ~93% | 36–58%, improves substantially with lag tolerance |

Lag-adjusted: 56% of predictions correct the same review, 72% within
two reviews, 76% within four. ~21% are genuine errors (never converted).

## Fixes that mattered, in order of impact

1. **Lookahead in membership checks.** Reading constituent status *at*
   the review date instead of the *prior* date silently guaranteed 0%
   recall — a stock added at date d is already in the holdings at d.
   Fixed by always reading membership from the snapshot strictly before
   the review being predicted.

2. **Data cut-off lag.** FTSE screens on data from ~2 months before the
   review takes effect (last business day of Jan/Apr/Jul/Oct), not
   review-date data. Using review-date data was lookahead. Fixing this
   alone took hit rate from **7% to 33%+** — the single largest fix in
   the project.

3. **General coverage percentile applied to fast-entry quarters.**
   June/December additions are governed by an absolute size-and-recency
   test (fast entry), not the general review's coverage percentile.
   Confirmed by an 11/11 clean split on listing age (see below).
   Building the correct dual-rule engine was worth roughly 15-20 points
   of hit rate on the June/December population specifically.

4. **Two 10x share-count errors** (TENNIND, INDIAMART) from yfinance,
   caught by cross-validating against NSE's own `issuedSize` field.
   Corrupted both the ranking and the coverage denominator until fixed.

5. **Missing 3-month minimum trading record rule** for new listings.
   Worth ~5 points of hit rate once added, using the true cut-off date
   (not the panel proxy date) for the listing-age test specifically —
   listing date is a fixed fact, so this one didn't need a proxy.

## Confirmed structural finding: fast entry is a different rule

| | n | Median listing age | % under 12 months |
|---|---|---|---|
| June/December adds | 11 | 5.5 months | 100% |
| March/September adds | 55 | 193 months | 5% |

Clean separation, no overlap. The four adds initially missed by a
coverage-percentile screen at June/December (JSW Infrastructure, Tata
Technologies, IREDA, Vishal Mega Mart) were all larger than many
correctly-caught March/September adds — they weren't too small, they
were being asked the wrong question.

## Tested and rejected — do not re-attempt without new evidence

- **Smallest-constituent floor as an entry test.** The smallest
  constituent (Relaxo Footwears, ~$686m) marks the *exit* boundary — a
  buffer-protected incumbent that shrank after being added — not the
  entry boundary. Using it as a floor let ~163 extra names through per
  review.
- **IMC (investable market cap) cumulation**, despite being what the
  published Ground Rules specify. Tested head-to-head against full
  market cap on identical data: ~73% recall vs ~95%+. Most likely cause:
  free-float data carries ~6pp mean absolute error against FTSE's own
  published investability weights (concentrated in strategic-holder
  names), and cumulating a noisy input compounds the error.
- **NSE's own IWF (Investible Weight Factor)** as a free-float
  replacement. More accurate than our computed free float (3.16pp mean
  error vs 6.04pp), but made no practical difference — the ranking uses
  full market cap, not IMC, so a better free-float number doesn't
  propagate into the actual decision.
- **True cut-off pricing** instead of the quarter-end proxy. Pulled
  price at the exact true cut-off date for the whole historical window
  and re-ran the backtest: recall *dropped* (94.5% → 83.6%). Confirmed
  not a bug (zero symbols were dropped, market cap totals matched within
  normal drift) — the boundary is dense enough (~25 companies per 1pp of
  coverage near the tail) that single-day price noise dominates over
  which specific day you price on. The simpler proxy is not a shortcut;
  it's equally good.
- **Foreign ownership headroom**, tested twice. First against 89 names
  split into confirmed true/false positives from the backtest — no
  separation (median headroom 91.2% false positives vs 89.2% true
  positives, essentially identical). Second against the model's current
  predicted removes using real Bloomberg data — all three names sat
  comfortably above the 10% danger zone. One real historical example
  found (HDFC Bank's absence from the index 2019–2023, explained
  precisely by low headroom until the 2023 HDFC Ltd merger diluted
  foreign ownership) confirms the *mechanism* is real, but it doesn't
  generalize to the population the model currently gets wrong.
- **PSU/government ownership pattern** (FACT, ITI, UCO Bank). Real and
  visible — these names pass every implemented screen and are never
  added — but free float alone doesn't separate them from genuine
  converts (never-added group actually had *higher* median free float
  than eventually-added names, because HDFC Bank and Kotak — data-gap
  false positives, not ownership-driven ones — were contaminating the
  comparison). Root cause not fully isolated. FACT remains the single
  most persistent unexplained false positive in the whole project.
- **Single- and multi-quarter rank momentum** as a feature to separate
  "will convert" from "will never convert." Both came back flat or
  backwards — the repeat-offender list (JK Cement, CRISIL, Fortis
  Healthcare, IndusInd Bank) turned out to be genuine future converts,
  not permanently excluded names, which is the opposite of what a
  momentum signal would need to detect.
- **Confidence-tier granularity beyond entry_coverage.** A train/test
  split-half check on the entry coverage threshold converged to the
  same value (87.8%) independently on both halves — real signal, kept.
  The same test was not repeated for `exit_coverage` or the fast-entry
  threshold; both remain provisional (see Known Limitations in README).

## Live forward validation

The fast-entry rule was checked against a real news report of FTSE's
actual June 2026 additions (not the backtest proxy): 6 of 6 reported
additions were caught (Tata Capital, Lenskart, LG Electronics India,
Meesho, ICICI Prudential AMC, Groww), with 2 additional predictions
(Anthem Biosciences, Ather Energy) unconfirmed either way by that
source. Genuinely out-of-sample — the rule was built before this
result existed.
