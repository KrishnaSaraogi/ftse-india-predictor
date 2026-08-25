# Required files (upload these from your Colab environment)

| File | Where it came from during development |
|---|---|
| `eligible_universe.csv` | Your Nifty 500 universe file |
| `constituents_baseline.csv` | Most recent real FLIN holdings - columns: `isin`, `name` |

Everything else in this folder (`nse_cache.json`, `daily_volume_panel.csv`)
is created and maintained automatically by the workflows. Do not create
these by hand - they need to be built by the fetchers so their format
matches exactly.
