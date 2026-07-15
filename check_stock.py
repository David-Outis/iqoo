#!/usr/bin/env python3
"""
iQOO Refurbished Phone Stock Checker
--------------------------------------
Checks a fixed list of iQOO refurbished-phone product pages plus the main
refurbished listing page. Sends a push notification (via ntfy.sh) whenever
a product goes from "out of stock" -> "in stock", or when a brand-new
refurbished listing appears on the main page.

Extra features:
- Retry logic: each page fetch retries up to 2 extra times (3 attempts total)
  before being treated as failed, so one flaky network blip doesn't cause a
  missed restock window.
- Heartbeat/failure alert: if EVERY tracked page fails for several runs in a
  row, sends one alert saying the checker may be broken (site structure
  changed, full block, etc.) instead of silently failing forever.
- Daily summary: once per IST day (after 8 AM IST), sends a digest listing
  current stock status for every tracked product, so you know the checker
  is alive without needing an actual restock.

State is persisted to state.json so we don't spam repeat notifications for
something that was already in stock last run.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path("state.json")

# ---- CONFIG ---------------------------------------------------------------

PRODUCT_URLS = [
    "https://shop.iqoo.com/in/product/2057",
    "https://shop.iqoo.com/in/product/2071",
    "https://shop.iqoo.com/in/product/2074",
    "https://shop.iqoo.com/in/product/2070",
    "https://shop.iqoo.com/in/product/2038",
    "https://shop.iqoo.com/in/product/2049",
    "https://shop.iqoo.com/in/product/2055",
    "https://shop.iqoo.com/in/product/2066",
]

LISTING_URL = "https://shop.iqoo.com/in/products/phone"

# ntfy.sh topic - set this as a GitHub Actions secret (NTFY_TOPIC)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# Retry settings
MAX_ATTEMPTS = 3          # 1 initial try + 2 retries
RETRY_BACKOFF_SECONDS = [3, 6]   # wait times between attempts

# Consecutive fully-failed runs before we send a "checker may be broken" alert
FAILURE_ALERT_THRESHOLD = 3

# Daily summary: only send it once per IST calendar day, and not before this
# IST hour (avoids sending it during the paused midnight-7am window)
SUMMARY_MIN_IST_HOUR = 8

# Weekly summary (NEW): sent once per ISO week, on this weekday (0=Monday),
# not before this IST hour. Reuses SUMMARY_MIN_IST_HOUR-style gating so it
# doesn't fire during the paused overnight window either.
WEEKLY_SUMMARY_WEEKDAY = 0  # Monday
WEEKLY_SUMMARY_MIN_IST_HOUR = 9

# How many days of restock/sold-out history to retain in state.json
HISTORY_RETENTION_DAYS = 30

IST = timezone(timedelta(hours=5, minutes=30))

# ---- STATE ------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---- NOTIFY -----------------------------------------------------------------

def notify(title, message, url=None, priority="urgent", tags="iphone,rotating_light"):
    print(f"[NOTIFY] {title} :: {message}")
    if not NTFY_URL:
        print("  (NTFY_TOPIC not set - skipping actual push notification)")
        return
    try:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        }
        if url:
            headers["Click"] = url
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=15)
    except Exception as e:
        print(f"  ! Failed to send notification: {e}")


# ---- HTTP WITH RETRIES --------------------------------------------------------

def fetch_with_retries(url):
    """
    GETs a URL with retry logic. Raises the last exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            print(f"  ! Attempt {attempt}/{MAX_ATTEMPTS} failed for {url}: {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
    raise last_exc


# ---- SCRAPE: single product page --------------------------------------------

def check_product(url):
    """
    Returns dict: { name, in_stock (bool), raw_status (str) }
    """
    resp = fetch_with_retries(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)

    # Product name: try og:title meta, fallback to <title>
    name = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        name = og_title["content"].strip()
    if not name and soup.title:
        name = soup.title.get_text(strip=True)
    if not name:
        name = url

    # Heuristics based on observed page text:
    # - "Out of stock" appears near the Add to cart button when unavailable
    # - Some out-of-stock pages instead show a "Notify me" CTA
    # - When available, none of the above appear and an active "Add to
    #   cart"/"Buy now" control is present instead.
    lowered = page_text.lower()

    out_of_stock = bool(re.search(r"out of stock", lowered))
    sold_out = bool(re.search(r"sold out", lowered))
    notify_me_cta = bool(re.search(r"notify me", lowered))
    # iQOO's shop shows the literal word "unavailable" right next to the
    # selected variant/buy button when that variant can't be purchased.
    # This was the actual marker on product 2070's page and was previously
    # unmatched, causing an out-of-stock page to be read as in stock.
    unavailable_cta = bool(re.search(r"\bunavailable\b", lowered))

    in_stock = not (out_of_stock or sold_out or notify_me_cta or unavailable_cta)

    if out_of_stock:
        raw_status = "Out of stock"
    elif sold_out:
        raw_status = "Sold out"
    elif notify_me_cta:
        raw_status = "Out of stock ('Notify me' button present)"
    elif unavailable_cta:
        raw_status = "Out of stock ('unavailable' marker present)"
    else:
        raw_status = "IN STOCK (no out-of-stock marker found)"

    # NEW: best-effort variant-level availability (see check_variants above).
    # Wrapped so any failure here can never affect the existing, working
    # page-level in_stock/raw_status result above.
    try:
        variants = check_variants(resp.text)
    except Exception as e:
        print(f"  ! Skipping variant detection (non-fatal): {e}")
        variants = []

    # If the page-level check already says the product is out of stock,
    # trust that over the swatch scan. iQOO doesn't consistently mark
    # individual colour/spec swatches as disabled even when the whole
    # product can't be purchased (seen on product 2057 - "Out of stock"
    # shown next to Add to cart, but all swatches still looked
    # selectable), which was causing every variant to be reported as
    # available and could trigger a false "variant available" alert.
    if not in_stock and variants:
        variants = [{**v, "available": False} for v in variants]

    return {"name": name, "in_stock": in_stock, "raw_status": raw_status, "variants": variants}


# ---- SCRAPE: variant-level availability (NEW) --------------------------------
#
# Best-effort addition on top of the existing (working) page-level check.
# iQOO's product page renders colour/storage "swatch" options as clickable
# elements; when a specific combination is unavailable, the site marks that
# option element as disabled (commonly via a `disable`/`disabled` class or
# an `aria-disabled="true"` attribute). This walks those option elements and
# reports which ones look disabled, so the notification can say e.g.
# "8GB+128GB Inferno Red" is back rather than just the product name.
#
# This is intentionally isolated from check_product()/in_stock logic: if the
# site's swatch markup doesn't match these heuristics, this simply returns
# an empty list and the rest of the script behaves exactly as before.

def check_variants(resp_text):
    """
    Returns a list of dicts: [{ "label": str, "available": bool }, ...]
    Best-effort; returns [] if no recognizable swatch markup is found.
    """
    variants = []
    try:
        soup = BeautifulSoup(resp_text, "html.parser")

        # Common patterns for swatch/option containers on this storefront
        # template family (vivo/iQOO shop uses a shared shopex frontend).
        candidates = soup.select(
            "[class*='sku'], [class*='spec'], [class*='color'], "
            "[class*='swatch'], [class*='option']"
        )

        seen_labels = set()
        for el in candidates:
            label = el.get_text(strip=True)
            if not label or len(label) > 60:
                continue
            # Skip obvious non-variant boilerplate
            if label.lower() in ("specification", "colour", "compare", "next"):
                continue
            if label in seen_labels:
                continue

            classes = " ".join(el.get("class", [])).lower()
            aria_disabled = str(el.get("aria-disabled", "")).lower() == "true"
            looks_disabled = (
                aria_disabled
                or "disable" in classes
                or "unavailable" in classes
                or "sold" in classes
            )

            # Only keep elements that look like actual selectable options
            # (have a disabled-capable class hook or aria attribute at all,
            # OR are short tokens typical of colour/storage labels).
            is_probable_variant = (
                aria_disabled
                or "disable" in classes
                or (el.name in ("li", "button", "div") and len(label.split()) <= 6)
            )
            if not is_probable_variant:
                continue

            seen_labels.add(label)
            variants.append({"label": label, "available": not looks_disabled})

    except Exception as e:
        print(f"  ! Variant-level parsing failed (non-fatal): {e}")
        return []

    return variants


# ---- SCRAPE: listing page for new refurbished entries -----------------------

def check_listing():
    """
    Returns a set of refurbished product titles currently shown on the
    main listing page (used to detect brand-new refurbished models that
    aren't in our tracked PRODUCT_URLS list at all).
    """
    resp = fetch_with_retries(LISTING_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    titles = set()
    for heading in soup.find_all(["h3", "h4"]):
        text = heading.get_text(strip=True)
        if not text:
            continue
        container = heading.find_parent()
        context_text = ""
        if container:
            imgs = container.find_all("img")
            context_text = " ".join(img.get("alt", "") for img in imgs)
        if "refurbished" in text.lower() or "refurbished" in context_text.lower():
            titles.add(text)

    return titles


# ---- WEEKLY ROLLUP (NEW) ------------------------------------------------------
#
# Builds a "this week: X restocked twice, both sold out within 10 minutes"
# style digest from the event history log. Purely additive: reads from
# state["history"] (a list the main loop appends "restock"/"soldout" events
# to) and never touches the existing product/listing/failure state.

def build_weekly_summary(history, now_ist):
    window_start = now_ist - timedelta(days=7)

    # Group events by product url, in chronological order
    by_url = {}
    for ev in history:
        try:
            ts = datetime.fromisoformat(ev["ts"])
        except Exception:
            continue
        if ts < window_start:
            continue
        by_url.setdefault(ev["url"], {"name": ev.get("name", ev["url"]), "events": []})
        by_url[ev["url"]]["events"].append((ts, ev["event"]))

    if not by_url:
        return None

    lines = []
    for url, info in by_url.items():
        events = sorted(info["events"], key=lambda e: e[0])
        restocks = [e for e in events if e[1] == "restock"]
        if not restocks:
            continue

        restock_count = len(restocks)
        durations = []
        for i, (ts, _) in enumerate(events):
            if events[i][1] != "restock":
                continue
            # find the next soldout after this restock
            for later_ts, later_ev in events[i + 1:]:
                if later_ev == "soldout":
                    durations.append(later_ts - ts)
                    break

        name = info["name"]
        if durations:
            fastest = min(durations)
            mins = int(fastest.total_seconds() // 60)
            if len(durations) == restock_count:
                lines.append(
                    f"- {name}: restocked {restock_count}x, sold out again "
                    f"within {mins} min (fastest)"
                )
            else:
                lines.append(
                    f"- {name}: restocked {restock_count}x this week "
                    f"({len(durations)} sold out again, fastest in {mins} min)"
                )
        else:
            lines.append(f"- {name}: restocked {restock_count}x this week, still in stock")

    if not lines:
        return None
    return "This week's restock activity:\n" + "\n".join(lines)


def prune_history(history, now_ist):
    cutoff = now_ist - timedelta(days=HISTORY_RETENTION_DAYS)
    kept = []
    for ev in history:
        try:
            ts = datetime.fromisoformat(ev["ts"])
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(ev)
    return kept


# ---- MAIN --------------------------------------------------------------------

def main():
    state = load_state()
    prev_products = state.get("products", {})
    prev_listing = set(state.get("listing_titles", []))
    consecutive_failures = state.get("consecutive_full_failures", 0)
    last_summary_date = state.get("last_summary_date", "")

    # NEW: variant-level state + event history for the weekly rollup
    prev_variants = state.get("variants", {})
    history = state.get("history", [])
    last_weekly_summary_date = state.get("last_weekly_summary_date", "")
    new_state_variants = {}

    new_state_products = {}
    product_errors = 0

    # 1. Check each tracked product page
    for url in PRODUCT_URLS:
        try:
            result = check_product(url)
        except Exception as e:
            print(f"! Giving up on {url} after retries: {e}")
            product_errors += 1
            # keep previous known state if request fails, don't wipe it
            if url in prev_products:
                new_state_products[url] = prev_products[url]
            continue

        name = result["name"]
        in_stock = result["in_stock"]
        raw_status = result["raw_status"]

        was_in_stock = prev_products.get(url, {}).get("in_stock", False)

        print(f"{name} ({url}): {raw_status}")

        if in_stock and not was_in_stock:
            notify(
                title=f"IN STOCK: {name}",
                message=f"{name} is now available! Tap to open (grab any color/variant that's in stock).\n{url}",
                url=url,
            )

        # NEW: log restock/soldout transitions for the weekly rollup.
        # Purely additive - doesn't affect the notify() call above.
        now_ist_iso = datetime.now(IST).isoformat()
        if in_stock and not was_in_stock:
            history.append({"ts": now_ist_iso, "url": url, "name": name, "event": "restock"})
        elif was_in_stock and not in_stock:
            history.append({"ts": now_ist_iso, "url": url, "name": name, "event": "soldout"})

        # NEW: variant-level availability comparison (best-effort).
        # If a specific colour/spec combo newly becomes available, call it
        # out by name. Falls back silently if variants couldn't be parsed.
        variants = result.get("variants", [])
        if variants:
            prev_variant_map = {
                v["label"]: v["available"] for v in prev_variants.get(url, [])
            }
            newly_available = [
                v["label"] for v in variants
                if v["available"] and not prev_variant_map.get(v["label"], False)
            ]
            # Only call out specific variants if not every one of them just
            # went from "no data" to available in one shot (first-ever run),
            # and only when the product itself wasn't already fully in stock
            # last run (avoids duplicate/noisy pings alongside the main
            # restock notification above).
            if newly_available and prev_variant_map and not (in_stock and was_in_stock):
                notify(
                    title=f"Variant available: {name}",
                    message=(
                        f"{name} - now available in: {', '.join(newly_available)}\n{url}"
                    ),
                    url=url,
                    priority="urgent",
                    tags="iphone,rotating_light",
                )
            new_state_variants[url] = variants
        elif url in prev_variants:
            # keep last known variant data if this run couldn't parse any
            new_state_variants[url] = prev_variants[url]

        new_state_products[url] = {"name": name, "in_stock": in_stock, "raw_status": raw_status}

    # 2. Check listing page for brand-new refurbished models
    listing_failed = False
    try:
        current_listing = check_listing()
        newly_seen = current_listing - prev_listing
        for title in newly_seen:
            notify(
                title="New Refurbished Model Listed",
                message=f"'{title}' just appeared on the iQOO refurbished store.\n{LISTING_URL}",
                url=LISTING_URL,
            )
        # Accumulate rather than replace: iQOO's listing page only shows a
        # subset of refurbished tiles at any given time (e.g. items seem to
        # drop off the listing when out of stock and reappear later), so
        # overwriting the saved set with just this run's snapshot caused
        # already-notified titles (like "Neo 10R Refurbished") to look
        # "new" again every time they flickered back into view.
        listing_titles_to_save = list(prev_listing | current_listing)
    except Exception as e:
        print(f"! Error checking listing page after retries: {e}")
        listing_failed = True
        listing_titles_to_save = list(prev_listing)

    # 3. Heartbeat / failure-alert logic
    total_checks = len(PRODUCT_URLS) + 1  # + listing page
    total_failures = product_errors + (1 if listing_failed else 0)
    fully_failed_run = total_failures == total_checks

    if fully_failed_run:
        consecutive_failures += 1
        print(f"All checks failed this run. Consecutive failed runs: {consecutive_failures}")
    else:
        if consecutive_failures >= FAILURE_ALERT_THRESHOLD:
            # We were in a failure streak and it just recovered
            notify(
                title="iQOO Stock Checker Recovered",
                message="The checker is back to fetching pages successfully after a failure streak.",
                priority="default",
                tags="white_check_mark",
            )
        consecutive_failures = 0

    if consecutive_failures == FAILURE_ALERT_THRESHOLD:
        # Fire exactly once when we cross the threshold (not every run after)
        notify(
            title="iQOO Stock Checker May Be Broken",
            message=(
                f"All tracked pages have failed to load for {consecutive_failures} "
                "consecutive runs. The site structure may have changed, or "
                "requests may be getting blocked. Check the GitHub Actions logs."
            ),
            priority="high",
            tags="warning",
        )

    # 4. Daily summary digest
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    if today_str != last_summary_date and now_ist.hour >= SUMMARY_MIN_IST_HOUR:
        lines = []
        for url in PRODUCT_URLS:
            info = new_state_products.get(url, {})
            name = info.get("name", url)
            status = info.get("raw_status", "Unknown (last check failed)")
            lines.append(f"- {name}: {status}")
        summary_msg = "Daily status check:\n" + "\n".join(lines)
        notify(
            title="iQOO Stock - Daily Summary",
            message=summary_msg,
            priority="low",
            tags="calendar",
        )
        last_summary_date = today_str

    # 5. Weekly rollup (NEW) - "this week: X restocked twice, sold out
    #    within N min" style digest, built from the history log above.
    if (
        today_str != last_weekly_summary_date
        and now_ist.weekday() == WEEKLY_SUMMARY_WEEKDAY
        and now_ist.hour >= WEEKLY_SUMMARY_MIN_IST_HOUR
    ):
        weekly_msg = build_weekly_summary(history, now_ist)
        if weekly_msg:
            notify(
                title="iQOO Stock - Weekly Summary",
                message=weekly_msg,
                priority="low",
                tags="calendar,bar_chart",
            )
        last_weekly_summary_date = today_str

    history = prune_history(history, now_ist)

    # ---- Save state ----
    state["products"] = new_state_products
    state["listing_titles"] = listing_titles_to_save
    state["consecutive_full_failures"] = consecutive_failures
    state["last_summary_date"] = last_summary_date
    # NEW state fields (additive - safe to ignore on old state.json files)
    state["variants"] = new_state_variants
    state["history"] = history
    state["last_weekly_summary_date"] = last_weekly_summary_date
    save_state(state)

    if product_errors or listing_failed:
        print(f"Completed with {product_errors} product error(s), listing_failed={listing_failed}.")

    print("Done.")


if __name__ == "__main__":
    main()
