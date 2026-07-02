#!/usr/bin/env python3
"""Export nutrition facts + ingredients for food products from Amazon.in / Flipkart.

Personal-use tool: give it a list of product page URLs and it scrapes each
page for the product name, brand, ingredients and nutrition information,
then writes one CSV row per product.

Usage:
    python export_nutrition.py --urls urls.txt --out nutrition.csv
    python export_nutrition.py https://www.amazon.in/dp/B0XXXXXXX --out nutrition.csv

Options:
    --urls FILE     Text file with one product URL per line (# comments allowed)
    --out FILE      Output CSV path (default: nutrition_export.csv)
    --delay SECS    Base delay between requests, jitter is added (default: 6)
    --playwright    Render pages with a headless browser (needs `pip install playwright`
                    + `playwright install chromium`). Use this if plain requests
                    get blocked with CAPTCHA pages.

Notes:
    * Both sites change their HTML frequently and rate-limit bots. This script
      is deliberately slow and polite (single-threaded, delays between fetches).
      Keep it that way and only use it on your own small product lists.
    * If a field can't be found, the row is still written with what was found
      plus the raw nutrition/ingredients text so nothing is silently lost.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies. Run: pip install -r requirements.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

CSV_COLUMNS = [
    "scraped_at",
    "source",
    "url",
    "product_name",
    "brand",
    "serving_size",
    "energy_kcal",
    "protein_g",
    "carbohydrate_g",
    "sugar_g",
    "total_fat_g",
    "saturated_fat_g",
    "trans_fat_g",
    "fiber_g",
    "sodium_mg",
    "ingredients",
    "nutrition_raw",
    "status",
]

# Spec-table labels that hold ingredient / nutrition text on both sites.
# (negative lookahead skips "Ingredient Type: Vegetarian"-style rows)
INGREDIENT_LABELS = re.compile(r"\bingredients?\b(?!\s+type)", re.I)
NUTRITION_LABELS = re.compile(
    r"nutrient content|nutrition(al)? (fact|info|value)|nutrition\b", re.I
)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_requests(url: str, session: requests.Session) -> str:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)  # let lazy-loaded spec sections render
        html = page.content()
        browser.close()
        return html


def looks_blocked(html: str) -> bool:
    markers = (
        "api-services-support@amazon.com",  # Amazon robot-check page
        "Enter the characters you see below",
        "captcha",
        "Retry in a moment",
    )
    low = html.lower()
    return any(m.lower() in low for m in markers) and "producttitle" not in low


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def table_rows(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """All (label, value) pairs from any table-ish structure on the page."""
    rows: list[tuple[str, str]] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            rows.append((clean(cells[0].get_text()), clean(cells[-1].get_text())))
    # Amazon detail bullets: <li><span class="a-text-bold">Label:</span> value</li>
    for li in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
        bold = li.find("span", class_="a-text-bold")
        if bold:
            label = clean(bold.get_text()).rstrip(":").replace("‏", "").replace("‎", "")
            value = clean(li.get_text().replace(bold.get_text(), "", 1)).lstrip(": ")
            rows.append((label, value))
    return rows


def find_by_label(rows: list[tuple[str, str]], pattern: re.Pattern) -> str:
    for label, value in rows:
        if pattern.search(label) and value:
            return value
    return ""


# --------------------------------------------------------------------------
# Site-specific extraction
# --------------------------------------------------------------------------

def parse_amazon(soup: BeautifulSoup) -> dict:
    data: dict[str, str] = {}
    title = soup.select_one("#productTitle")
    data["product_name"] = clean(title.get_text()) if title else ""

    byline = soup.select_one("#bylineInfo")
    if byline:
        data["brand"] = clean(
            re.sub(r"^(Visit the|Brand:)\s*|\s*Store$", "", clean(byline.get_text()))
        )

    rows = table_rows(soup)
    if not data.get("brand"):
        data["brand"] = find_by_label(rows, re.compile(r"^brand$", re.I))

    ingredients = find_by_label(rows, INGREDIENT_LABELS)
    nutrition = find_by_label(rows, NUTRITION_LABELS)

    # "Important information" block often carries Ingredients / Nutrition headings.
    for section in soup.select("#important-information .content, #importantInformation .content"):
        heading = section.find(["h4", "h5", "strong"])
        head_text = clean(heading.get_text()) if heading else ""
        body = clean(section.get_text().replace(head_text, "", 1))
        if INGREDIENT_LABELS.search(head_text) and not ingredients:
            ingredients = body
        elif NUTRITION_LABELS.search(head_text) and not nutrition:
            nutrition = body

    # A+ content sometimes holds a nutrition table with no useful heading.
    if not nutrition:
        aplus = soup.select_one("#aplus, #aplus_feature_div")
        if aplus:
            m = re.search(
                r"(nutritional? (?:facts|information|value)s?.{0,1200})",
                clean(aplus.get_text()),
                re.I,
            )
            if m:
                nutrition = m.group(1)

    data["ingredients"] = ingredients
    data["nutrition_raw"] = nutrition
    return data


def parse_flipkart(soup: BeautifulSoup) -> dict:
    data: dict[str, str] = {}
    # Flipkart renames CSS classes constantly; try known title selectors, then h1.
    for sel in ("span.B_NuCI", "span.VU-ZEz", "h1 span", "h1"):
        node = soup.select_one(sel)
        if node and clean(node.get_text()):
            data["product_name"] = clean(node.get_text())
            break

    rows = table_rows(soup)
    # Spec rows rendered as sibling divs rather than <tr> in some layouts.
    for row in soup.select("div[class] > div[class]"):
        kids = row.find_all("div", recursive=False)
        if len(kids) == 2:
            label, value = clean(kids[0].get_text()), clean(kids[1].get_text())
            if label and value and len(label) < 60:
                rows.append((label, value))

    data["brand"] = find_by_label(rows, re.compile(r"^brand$", re.I))
    data["ingredients"] = find_by_label(rows, INGREDIENT_LABELS)
    data["nutrition_raw"] = find_by_label(rows, NUTRITION_LABELS)

    # Fallback: scan whole page text for an "Ingredients: ..." run.
    if not data["ingredients"]:
        m = re.search(r"Ingredients?\s*[:\-]\s*(.{10,800}?)(?:\.\s[A-Z]|$)",
                      clean(soup.get_text()), re.I)
        if m:
            data["ingredients"] = clean(m.group(1))
    return data


# --------------------------------------------------------------------------
# Nutrient parsing from raw text
# --------------------------------------------------------------------------

NUM = r"([\d]+(?:[.,]\d+)?)"

NUTRIENT_PATTERNS = {
    "energy_kcal": rf"energy[^0-9]{{0,20}}{NUM}\s*k?cal",
    "protein_g": rf"protein[^0-9]{{0,20}}{NUM}\s*g",
    "carbohydrate_g": rf"carbohydrates?[^0-9]{{0,20}}{NUM}\s*g",
    "sugar_g": rf"(?:total\s+)?sugars?[^0-9]{{0,20}}{NUM}\s*g",
    "total_fat_g": rf"(?:total\s+)?fat[^0-9]{{0,20}}{NUM}\s*g",
    "saturated_fat_g": rf"saturated\s+fat[^0-9]{{0,20}}{NUM}\s*g",
    "trans_fat_g": rf"trans\s+fat[^0-9]{{0,20}}{NUM}\s*g",
    "fiber_g": rf"(?:dietary\s+)?fibre?[^0-9]{{0,20}}{NUM}\s*g",
    "sodium_mg": rf"sodium[^0-9]{{0,20}}{NUM}\s*mg",
}

SERVING_PATTERN = re.compile(
    r"(?:per|serving size)[^0-9]{0,15}(\d+(?:\.\d+)?\s*(?:g|gm|grams?|ml))", re.I
)


def parse_nutrients(raw: str) -> dict:
    out = {}
    for key, pattern in NUTRIENT_PATTERNS.items():
        m = re.search(pattern, raw, re.I)
        if m:
            out[key] = m.group(1).replace(",", ".")
    m = SERVING_PATTERN.search(raw)
    if m:
        out["serving_size"] = clean(m.group(1))
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "amazon." in host:
        return "amazon"
    if "flipkart." in host:
        return "flipkart"
    return ""


def scrape(url: str, session: requests.Session, use_playwright: bool) -> dict:
    row = {c: "" for c in CSV_COLUMNS}
    row["url"] = url
    row["scraped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    source = detect_source(url)
    row["source"] = source
    if not source:
        row["status"] = "skipped: not an amazon/flipkart URL"
        return row

    try:
        html = fetch_playwright(url) if use_playwright else fetch_requests(url, session)
    except Exception as exc:  # noqa: BLE001 - record and move on
        row["status"] = f"fetch error: {exc}"
        return row

    if looks_blocked(html):
        row["status"] = "blocked (CAPTCHA/robot check) — try --playwright or wait"
        return row

    soup = BeautifulSoup(html, "html.parser")
    data = parse_amazon(soup) if source == "amazon" else parse_flipkart(soup)
    row.update({k: v for k, v in data.items() if v})

    combined = " ".join(filter(None, [row["nutrition_raw"], row["ingredients"]]))
    row.update(parse_nutrients(combined))

    if row["product_name"]:
        missing = [f for f in ("ingredients", "nutrition_raw") if not row[f]]
        row["status"] = "ok" if not missing else f"partial: missing {', '.join(missing)}"
    else:
        row["status"] = "parse error: product name not found (page layout changed?)"
    return row


def read_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.url)
    if args.urls:
        with open(args.urls, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    return urls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="*", help="Product URLs")
    parser.add_argument("--urls", help="File with one URL per line")
    parser.add_argument("--out", default="nutrition_export.csv")
    parser.add_argument("--delay", type=float, default=6.0)
    parser.add_argument("--playwright", action="store_true",
                        help="Render pages in a headless browser")
    args = parser.parse_args()

    urls = read_urls(args)
    if not urls:
        parser.error("No URLs given. Pass them as arguments or via --urls FILE.")

    session = requests.Session()
    rows = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        row = scrape(url, session, args.playwright)
        print(f"    -> {row['status']}")
        rows.append(row)
        if i < len(urls):
            time.sleep(args.delay + random.uniform(0, 3))

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    ok = sum(1 for r in rows if r["status"].startswith(("ok", "partial")))
    print(f"\nWrote {len(rows)} rows ({ok} with data) to {args.out}")


if __name__ == "__main__":
    main()
