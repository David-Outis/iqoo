# iQOO Refurbished Stock Checker

Checks these pages on a schedule and pushes a phone notification the moment
something comes back in stock:

- https://shop.iqoo.com/in/product/2057
- https://shop.iqoo.com/in/product/2071
- https://shop.iqoo.com/in/product/2074
- https://shop.iqoo.com/in/product/2070
- https://shop.iqoo.com/in/product/2038
- https://shop.iqoo.com/in/products/phone (catches brand-new refurbished models too)

## How it works

- `check_stock.py` fetches each page and looks for "Out of stock" / "Sold out"
  text. If a product was previously out of stock and is now not, it fires a
  notification. It also watches the main listing page for any *new*
  refurbished model appearing that isn't already in the tracked list.
- `state.json` remembers the last known status so you're not notified over
  and over for the same in-stock item — only on the out-of-stock -> in-stock
  transition.
- GitHub Actions runs this on a schedule (see below), and commits the updated
  `state.json` back to the repo each run so state persists between runs.
- Notifications are sent via **ntfy.sh** — a free, no-signup push notification
  service. You just install the ntfy app and subscribe to a topic name you
  pick.

## One-time setup (~5 minutes)

1. **Create a new GitHub repo** (private is fine) and upload all these files
   (`check_stock.py`, `requirements.txt`, `state.json`, `.github/workflows/stock-check.yml`, this README) preserving the folder structure.

2. **Install the ntfy app** on your phone:
   - Android: https://play.google.com/store/apps/details?id=io.heckel.ntfy
   - iOS: https://apps.apple.com/us/app/ntfy/id1625396347

3. **Pick a secret topic name**, e.g. `iqoo-stock-yourname-8231` (make it
   unique/hard to guess — anyone who knows your topic name can also send to
   or read it, since ntfy.sh is a public broker). In the ntfy app, tap **+**
   and subscribe to that exact topic name.

4. **Add the topic as a GitHub secret**:
   - In your repo: Settings -> Secrets and variables -> Actions -> New repository secret
   - Name: `NTFY_TOPIC`
   - Value: the topic name you picked, e.g. `iqoo-stock-yourname-8231`

5. **Enable the workflow**: Go to the "Actions" tab in your repo, and enable
   workflows if prompted. The schedule will start running automatically.
   You can also click "Run workflow" to trigger it manually right away to
   test everything works (you should see a test isn't sent unless something
   is actually in stock — check the Actions log output to confirm it ran
   without errors).

6. **Test the notification path** (optional but recommended): temporarily
   run this locally or via workflow_dispatch with logging - the Actions log
   will show `[NOTIFY] ...` lines only when a real state change happens. To
   force-test notifications work, you can temporarily edit `state.json` to
   mark one product as `"in_stock": false"` when it's actually in stock, run
   the workflow once, then revert.

## Schedule

Runs every 10 minutes from 7:00 AM to 12:00 AM (midnight) IST, and is paused
from 12:00 AM to 7:00 AM IST, exactly as requested. All cron times in the
workflow file are UTC; IST = UTC + 5:30.

## Why GitHub Actions won't get blocked

- Each run uses a fresh, rotating Microsoft/GitHub-owned IP (not a personal
  or datacenter IP tied to you), and requests are spaced 10 minutes apart —
  far below anything that would look like scraping abuse.
- The script sends a normal desktop browser User-Agent header.
- If iQOO ever does rate-limit or block a specific run, the script logs the
  error and keeps last-known state rather than crashing the whole workflow,
  so a single blip won't break your alerts.

## Adjusting things later

- **Add/remove tracked products**: edit the `PRODUCT_URLS` list in
  `check_stock.py`.
- **Change frequency**: edit the `cron` lines in
  `.github/workflows/stock-check.yml`. Cron time is UTC.
- **Change active hours**: same file — the three cron lines together define
  the active window (currently 07:00-24:00 IST every 10 min).
