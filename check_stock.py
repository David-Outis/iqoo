#!/usr/bin/env python3
"""
iQOO Refurbished Phone Stock Checker
--------------------------------------
Checks a fixed list of iQOO refurbished-phone product pages plus the main
refurbished listing page. Sends a push notification (via ntfy.sh) whenever
a product goes from "out of stock" -> "in stock", or when a brand-new
refurbished listing appears on the main page.

State is persisted to state.json so we don't spam repeat notifications for
something that was already in stock last run.
"""

import json
import os
import re
import sys
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

def notify(title, message, url=None):
    print(f"[NOTIFY] {title} :: {message}")
    if not NTFY_URL:
        print("  (NTFY_TOPIC not set - skipping actual push notification)")
        return
    try:
        headers = {
            "Title": title,
            "Priority": "urgent",
            "Tags": "iphone,rotating_light",
        }
        if url:
            headers["Click"] = url
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=15)
    except Exception as e:
        print(f"  ! Failed to send notification: {e}")


# ---- SCRAPE: single product page --------------------------------------------

def check_product(url):
    """
    Returns dict: { name, in_stock (bool), raw_status (str) }
    """
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
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
    # - When available, that text is absent and an active "Add to cart" /
    #   "Buy now" control is present instead.
    lowered = page_text.lower()

    out_of_stock = bool(re.search(r"out of stock", lowered))
    sold_out = bool(re.search(r"sold out", lowered))

    in_stock = not (out_of_stock or sold_out)

    if out_of_stock:
        raw_status = "Out of stock"
    elif sold_out:
        raw_status = "Sold out"
    else:
        raw_status = "IN STOCK (no out-of-stock marker found)"

    return {"name": name, "in_stock": in_stock, "raw_status": raw_status}


# ---- SCRAPE: listing page for new refurbished entries -----------------------

def check_listing():
    """
    Returns a set of refurbished product titles currently shown on the
    main listing page (used to detect brand-new refurbished models that
    aren't in our tracked PRODUCT_URLS list at all).
    """
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    titles = set()
    # Each product card seems to have a heading (h3/h4) with the model name,
    # and nearby "Refurbished" badge alt text. We scan headings whose text
    # (or a sibling image alt) mentions "Refurbished".
    for heading in soup.find_all(["h3", "h4"]):
        text = heading.get_text(strip=True)
        if not text:
            continue
        # look at surrounding container for a "Refurbished" badge
        container = heading.find_parent()
        context_text = ""
        if container:
            imgs = container.find_all("img")
            context_text = " ".join(img.get("alt", "") for img in imgs)
        if "refurbished" in text.lower() or "refurbished" in context_text.lower():
            titles.add(text)

    return titles


# ---- MAIN --------------------------------------------------------------------

def main():
    state = load_state()
    prev_products = state.get("products", {})
    prev_listing = set(state.get("listing_titles", []))

    new_state_products = {}
    any_error = False

    # 1. Check each tracked product page
    for url in PRODUCT_URLS:
        try:
            result = check_product(url)
        except Exception as e:
            print(f"! Error checking {url}: {e}")
            any_error = True
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
                message=f"{name} is now available!\n{url}",
                url=url,
            )

        new_state_products[url] = {"name": name, "in_stock": in_stock}

    # 2. Check listing page for brand-new refurbished models
    try:
        current_listing = check_listing()
        newly_seen = current_listing - prev_listing
        for title in newly_seen:
            notify(
                title="New Refurbished Model Listed",
                message=f"'{title}' just appeared on the iQOO refurbished store.\n{LISTING_URL}",
                url=LISTING_URL,
            )
        listing_titles_to_save = list(current_listing)
    except Exception as e:
        print(f"! Error checking listing page: {e}")
        any_error = True
        listing_titles_to_save = list(prev_listing)

    state["products"] = new_state_products
    state["listing_titles"] = listing_titles_to_save
    save_state(state)

    if any_error:
        # Don't fail the whole workflow (that would email you failure spam
        # instead of stock alerts) - just log it.
        print("Completed with some errors (see above).")

    print("Done.")


if __name__ == "__main__":
    main()
