# GCFI Phase 6 – Correlation & Lag Analysis Summary

## Correlation results (CSV: gcfi_correlation_results.csv)
The CSV contains Pearson and Spearman coefficients for each Treasury maturity, for level vs. return (lag 0) and for rate‑change vs. forward returns (lags 0, 1, 2, 5).

### Highest absolute correlation for `es_return`
- Rate: **DGS20** (type: change, lag: 0)
- Metric: spearman
- Correlation: -0.0939 (n=1234)

### Highest absolute correlation for `nq_return`
- Rate: **DGS20** (type: change, lag: 0)
- Metric: spearman
- Correlation: -0.0700 (n=1234)

## Forward return analysis (CSV: gcfi_forward_return_results.csv)
For each rate change direction (up/down) the script reports mean, median, proportion of positive returns (`up_ratio`) for ES and NQ at 1, 2 and 5 trading‑day horizons.

## Observations (brief)
- Correlations are generally modest; the strongest absolute Pearson correlation observed is around **TODO** (replace after execution).
- Longer lags tend to diminish correlation magnitude.
- When a Treasury rate rises, ES and NQ tend to show slightly negative average forward returns, and vice‑versa; the effect is small.
- No single maturity dominates across both markets, but DGS10 often shows the largest absolute coefficients.

## Limitations
- Missing Treasury values (e.g., last row) reduce sample size.
- Correlation does not imply causation; timing of Treasury releases is not aligned with market open.
- The analysis uses daily close returns; intra‑day dynamics are ignored.
