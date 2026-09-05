# Execution analytics — drop-in for the `Harsh` repo

Copy this `analytics/` folder into the root of the `Harsh` repo.

## Files
- `build_history.py` — reads Sheet1, Trades, and DV_History from your
  "Monthly Breakout Scan" Google Sheet, joins them, and writes
  `data/history.json` with every signal ever flagged plus:
  - whether it was ever actually traded
  - immediate-entry vs actual-entry return comparison
  - loophole flags (never_entered, archived_before_entry, large_entry_slippage,
    long_wait, waiting_cost_you, target_never_reached, duplicate_signal, no_price_history)
- `dashboard.html` — self-contained (Chart.js via CDN) dashboard that reads
  `data/history.json` and renders it. No build step — open it directly or
  host it (e.g. GitHub Pages, or drop it into your existing Vercel/static
  deploy).

## Run it

```bash
export GOOGLE_SERVICE_ACCOUNT_KEY='<the same JSON key your other scripts use>'
pip install gspread google-auth
python analytics/build_history.py
```

This writes `data/history.json` to the repo root. Commit it (or let a
GitHub Action commit it, same pattern as `sync_dashboard.py`).

## Wire it into your existing daily workflow

Add a step to `.github/workflows/*.yml`, **after** `sync_dashboard.py` and
`delivery_value.py` have run (it depends on Sheet1 and DV_History being
fresh):

```yaml
      - name: Build execution history
        run: python analytics/build_history.py
        env:
          GOOGLE_SERVICE_ACCOUNT_KEY: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_KEY }}

      - name: Commit history.json
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add data/history.json
          git diff --staged --quiet || git commit -m "Update execution history"
          git push
```

## View the dashboard

Open `analytics/dashboard.html` in a browser (it fetches `data/history.json`
via a relative path, so serve it from the repo root — e.g.
`python -m http.server` locally, or host both files together on GitHub
Pages/Vercel). If you host the dashboard somewhere separate from the data,
edit the `DATA_URL` constant near the top of the `<script>` tag to point at
the raw GitHub URL instead.

## What "loophole" means here, concretely

The most important number in `summary.execution_rate_pct` is usually the
biggest gap: this system currently flags far more candidates than you
actually trade (in the sample data: 358 flagged, 24 traded — a 6.7%
execution rate). That's not necessarily wrong (you're filtering for
quality), but it's worth knowing precisely, and the dashboard's "execution
funnel" chart makes it visible over time instead of buried in a sheet.
