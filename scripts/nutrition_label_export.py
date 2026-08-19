#!/usr/bin/env python3
"""Export nutrition facts + ingredients for food products into a CSV.

Supports Amazon.in, Flipkart, and Blinkit. For most food items the nutrition
facts and ingredients are printed on the back-of-pack label photo rather than
written on the page, so this script:

  1. Collects product URLs — either ones you give it, or by crawling a food
     category / search listing page on any of the three sites.
  2. Opens each product page and grabs (a) any nutrition/ingredients text on
     the page itself and (b) the product gallery images.
  3. Sends the gallery images to Claude (vision), which identifies the
     back-of-pack label image and reads the nutrition table + ingredients
     list off it.
  4. Writes everything to a CSV, one row per product.

Usage:
    # Everything in a category (crawls the listing, then each product):
    python nutrition_label_export.py \\
        --category "https://www.amazon.in/s?rh=n%3A2454178031" \\
        --category "https://www.flipkart.com/food-products/pr?sid=eat" \\
        --max-items 50 --out nutrition.csv

    # Specific products:
    python nutrition_label_export.py --urls urls.txt --out nutrition.csv
    python nutrition_label_export.py "https://www.amazon.in/dp/B0XXXXXXX"

Setup:
    pip install requests beautifulsoup4 anthropic playwright
    playwright install chromium
    export ANTHROPIC_API_KEY=sk-ant-...   # needed for reading label images

Options:
    --category URL   Category/search listing to crawl (repeatable)
    --urls FILE      File with one product URL per line (# comments ok)
    --out FILE       Output CSV (default nutrition_export.csv)
    --max-items N    Cap on products collected per category (default 50)
    --max-images N   Gallery images sent to Claude per product (default 8)
    --model ID       Claude model for label reading (default claude-opus-4-8)
    --delay SECS     Base delay between product fetches, jitter added (default 5)
    --no-vision      Skip label-image reading; page text only
    --lat / --lon    Location for Blinkit (default: Bengaluru). Blinkit only
                     shows products it can deliver to this location.

Notes:
    * Playwright is strongly recommended (required for Blinkit and for
      category crawling). Without it the script falls back to plain HTTP,
      which only works for direct Amazon/Flipkart product URLs and gets
      blocked more often.
    * The script is deliberately slow and single-threaded. These sites
      rate-limit bots; keep it polite and use it for personal lists, not
      site-scale scraping (which is against their terms of service).
    * Rows are always written, even on failure, with a status column so you
      can retry just the failed URLs.
"""

from __future__ import annotations

import argparse
import base64
import csv
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies. Run: pip install requests beautifulsoup4 anthropic playwright")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

CSV_COLUMNS = [
    "scraped_at", "source", "url", "product_name", "brand",
    "serving_size", "energy_kcal", "protein_g", "carbohydrate_g", "sugar_g",
    "total_fat_g", "saturated_fat_g", "trans_fat_g", "fiber_g", "sodium_mg",
    "ingredients", "nutrition_raw", "label_image_url", "data_source", "status",
]

NUTRIENT_KEYS = [
    "serving_size", "energy_kcal", "protein_g", "carbohydrate_g", "sugar_g",
    "total_fat_g", "saturated_fat_g", "trans_fat_g", "fiber_g", "sodium_mg",
]

INGREDIENT_LABELS = re.compile(r"\bingredients?\b(?!\s+type)", re.I)
NUTRITION_LABELS = re.compile(
    r"nutrient content|nutrition(al)? (fact|info|value)|nutrition\b", re.I
)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for site in ("amazon", "flipkart", "blinkit"):
        if site in host:
            return site
    return ""


# --------------------------------------------------------------------------
# Fetching: Playwright browser (preferred) with plain-requests fallback
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, lat: float, lon: float):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._ctx = None
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            browser = self._pw.chromium.launch(headless=True)
            self._ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 900},
                geolocation={"latitude": lat, "longitude": lon},
                permissions=["geolocation"],
                locale="en-IN",
            )
            # Best-effort location hint for Blinkit (hyperlocal delivery app).
            for name in ("lat", "gr_1_lat"):
                self._ctx.add_cookies([{"name": name, "value": str(lat),
                                        "domain": ".blinkit.com", "path": "/"}])
            for name in ("lon", "gr_1_lon"):
                self._ctx.add_cookies([{"name": name, "value": str(lon),
                                        "domain": ".blinkit.com", "path": "/"}])
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Playwright unavailable ({exc}); falling back to plain HTTP.")
            print("[warn] Blinkit and category crawling need Playwright:"
                  " pip install playwright && playwright install chromium")

    @property
    def has_browser(self) -> bool:
        return self._ctx is not None

    def html(self, url: str, scroll_rounds: int = 0) -> str:
        if self._ctx is None:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
            for _ in range(scroll_rounds):
                page.mouse.wheel(0, 2_400)
                page.wait_for_timeout(1_000)
            return page.content()
        finally:
            page.close()

    def download(self, url: str) -> tuple[bytes, str] | None:
        """Fetch an image; returns (bytes, media_type) or None."""
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.content
            if not data or len(data) > 4_500_000:
                return None
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                ctype = "image/png" if url.lower().endswith(".png") else "image/jpeg"
            return data, ctype
        except Exception:  # noqa: BLE001
            return None


def looks_blocked(html: str) -> bool:
    low = html.lower()
    markers = ("api-services-support@amazon.com",
               "enter the characters you see below", "captcha")
    return any(m in low for m in markers) and "producttitle" not in low


# --------------------------------------------------------------------------
# Category crawling → product URLs
# --------------------------------------------------------------------------

def product_links(html: str, base_url: str, source: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        url = None
        if source == "amazon":
            m = re.search(r"/dp/([A-Z0-9]{10})", href)
            if m:
                url = f"https://www.amazon.in/dp/{m.group(1)}"
        elif source == "flipkart" and "/p/itm" in href:
            url = urljoin(base_url, href).split("&lid=")[0]
        elif source == "blinkit" and "/prn/" in href:
            url = urljoin(base_url, href)
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links


def crawl_category(fetcher: Fetcher, url: str, max_items: int) -> list[str]:
    source = detect_source(url)
    if not source:
        print(f"[skip] Not an Amazon/Flipkart/Blinkit URL: {url}")
        return []
    if not fetcher.has_browser:
        print(f"[skip] Category crawling requires Playwright: {url}")
        return []

    collected: list[str] = []
    if source == "blinkit":
        # Infinite-scroll listing: one load, scroll until enough items.
        html = fetcher.html(url, scroll_rounds=15)
        collected = product_links(html, url, source)[:max_items]
    else:
        # Amazon/Flipkart paginate with a page= query param.
        sep = "&" if "?" in url else "?"
        for page_no in range(1, 21):
            page_url = url if page_no == 1 else f"{url}{sep}page={page_no}"
            html = fetcher.html(page_url)
            found = [u for u in product_links(html, page_url, source)
                     if u not in collected]
            if not found:
                break
            collected.extend(found)
            if len(collected) >= max_items:
                collected = collected[:max_items]
                break
            time.sleep(3 + random.uniform(0, 2))
    print(f"[category] {len(collected)} products from {url}")
    return collected


# --------------------------------------------------------------------------
# Product page parsing (text + gallery images)
# --------------------------------------------------------------------------

def table_rows(soup: BeautifulSoup) -> list[tuple[str, str]]:
    rows = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            rows.append((clean(cells[0].get_text()), clean(cells[-1].get_text())))
    for li in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
        bold = li.find("span", class_="a-text-bold")
        if bold:
            label = clean(bold.get_text()).rstrip(":").replace("‏", "").replace("‎", "")
            value = clean(li.get_text().replace(bold.get_text(), "", 1)).lstrip(": ")
            rows.append((label, value))
    # Flipkart/Blinkit often render spec rows as sibling divs.
    for div in soup.select("div[class] > div[class]"):
        kids = div.find_all("div", recursive=False)
        if len(kids) == 2:
            label, value = clean(kids[0].get_text()), clean(kids[1].get_text())
            if label and value and len(label) < 60:
                rows.append((label, value))
    return rows


def find_by_label(rows: list[tuple[str, str]], pattern: re.Pattern) -> str:
    for label, value in rows:
        if pattern.search(label) and value:
            return value
    return ""


def parse_page_text(soup: BeautifulSoup, source: str) -> dict:
    data = {"product_name": "", "brand": "", "ingredients": "", "nutrition_raw": ""}

    if source == "amazon":
        node = soup.select_one("#productTitle")
        data["product_name"] = clean(node.get_text()) if node else ""
        byline = soup.select_one("#bylineInfo")
        if byline:
            data["brand"] = clean(re.sub(r"^(Visit the|Brand:)\s*|\s*Store$", "",
                                         clean(byline.get_text())))
    else:
        for sel in ("span.B_NuCI", "span.VU-ZEz", "h1 span", "h1"):
            node = soup.select_one(sel)
            if node and clean(node.get_text()):
                data["product_name"] = clean(node.get_text())
                break

    rows = table_rows(soup)
    data["brand"] = data["brand"] or find_by_label(rows, re.compile(r"^brand$", re.I))
    data["ingredients"] = find_by_label(rows, INGREDIENT_LABELS)
    data["nutrition_raw"] = find_by_label(rows, NUTRITION_LABELS)

    # Amazon "Important information" block.
    for section in soup.select("#important-information .content, #importantInformation .content"):
        heading = section.find(["h4", "h5", "strong"])
        head = clean(heading.get_text()) if heading else ""
        body = clean(section.get_text().replace(head, "", 1))
        if INGREDIENT_LABELS.search(head) and not data["ingredients"]:
            data["ingredients"] = body
        elif NUTRITION_LABELS.search(head) and not data["nutrition_raw"]:
            data["nutrition_raw"] = body

    # Fallback: scan full page text (works for Blinkit's rendered sections).
    text = clean(soup.get_text(" "))
    if not data["ingredients"]:
        m = re.search(r"Ingredients?\s*[:\-]?\s+(.{15,800}?)(?:\s{2,}|Nutrition|Shelf Life|Disclaimer|$)",
                      text, re.I)
        if m:
            data["ingredients"] = clean(m.group(1))
    if not data["nutrition_raw"]:
        m = re.search(r"(Nutrition(?:al)?\s+(?:Information|Facts|Value)s?.{20,1200}?)"
                      r"(?:Ingredients|Shelf Life|Disclaimer|Customer|$)", text, re.I)
        if m:
            data["nutrition_raw"] = clean(m.group(1))
    return data


def gallery_images(html: str, soup: BeautifulSoup, source: str, max_images: int) -> list[str]:
    urls: list[str] = []

    def add(u: str):
        if u and u.startswith("http") and u not in urls:
            urls.append(u)

    if source == "amazon":
        for m in re.finditer(r'"hiRes":"(https://[^"]+)"', html):
            add(m.group(1))
        for m in re.finditer(r'"large":"(https://[^"]+)"', html):
            add(m.group(1))
    elif source == "flipkart":
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "flixcart.com/image/" in src:
                # Upgrade thumbnails to a readable resolution.
                src = re.sub(r"/image/\d+/\d+/", "/image/832/832/", src)
                src = re.sub(r"[?&]q=\d+", "?q=90", src)
                add(src)
    elif source == "blinkit":
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "cdn.grofers.com" in src or "cdn.blinkit.com" in src:
                add(src)
    return urls[:max_images]


# --------------------------------------------------------------------------
# Label reading with Claude vision
# --------------------------------------------------------------------------

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "label_found": {"type": "boolean"},
        "label_image_index": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "description": "1-based index of the image showing the nutrition/ingredients label",
        },
        "serving_size": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "energy_kcal": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "protein_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "carbohydrate_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "sugar_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "total_fat_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "saturated_fat_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "trans_fat_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "fiber_g": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "sodium_mg": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "ingredients": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "nutrition_text": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Full nutrition table transcribed from the label, as plain text",
        },
    },
    "required": ["label_found", "label_image_index", "serving_size", "energy_kcal",
                 "protein_g", "carbohydrate_g", "sugar_g", "total_fat_g",
                 "saturated_fat_g", "trans_fat_g", "fiber_g", "sodium_mg",
                 "ingredients", "nutrition_text"],
    "additionalProperties": False,
}

LABEL_PROMPT = """\
These are the gallery images of a packaged food product sold online.

1. Find the image(s) showing the back-of-pack label with the nutrition facts \
table and/or the ingredients list.
2. Transcribe the ingredients list exactly as printed.
3. Read the nutrition table and fill in the per-100g values where available \
(if only per-serving values are printed, use those and record the serving \
size). Report numbers exactly as printed, without units.
4. If no image shows nutrition or ingredient information, set label_found to \
false and everything else to null. Never guess values that are not readable.\
"""


class LabelReader:
    def __init__(self, model: str):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.anthropic = anthropic

    def read(self, fetcher: Fetcher, image_urls: list[str]) -> dict | None:
        content, kept_urls = [], []
        for url in image_urls:
            img = fetcher.download(url)
            if not img:
                continue
            data, media_type = img
            kept_urls.append(url)
            content.append({"type": "text", "text": f"Image {len(kept_urls)}:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode(),
                },
            })
        if not kept_urls:
            return None
        content.append({"type": "text", "text": LABEL_PROMPT})

        import json
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": LABEL_SCHEMA}},
            )
        except self.anthropic.APIError as exc:
            print(f"    [vision] API error: {exc}")
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            return None
        result = json.loads(text)
        idx = result.get("label_image_index")
        if isinstance(idx, int) and 1 <= idx <= len(kept_urls):
            result["label_image_url"] = kept_urls[idx - 1]
        return result


# --------------------------------------------------------------------------
# Nutrient regex fallback (for page text when no label was readable)
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
    r"(?:per|serving size)[^0-9]{0,15}(\d+(?:\.\d+)?\s*(?:g|gm|grams?|ml))", re.I)


def parse_nutrients_from_text(raw: str) -> dict:
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
# Per-product pipeline
# --------------------------------------------------------------------------

def scrape_product(fetcher: Fetcher, reader: LabelReader | None,
                   url: str, max_images: int) -> dict:
    row = {c: "" for c in CSV_COLUMNS}
    row["url"] = url
    row["scraped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    source = detect_source(url)
    row["source"] = source
    if not source:
        row["status"] = "skipped: unsupported site"
        return row

    try:
        html = fetcher.html(url)
    except Exception as exc:  # noqa: BLE001
        row["status"] = f"fetch error: {exc}"
        return row
    if looks_blocked(html):
        row["status"] = "blocked (CAPTCHA/robot check) — wait and retry"
        return row

    soup = BeautifulSoup(html, "html.parser")
    page = parse_page_text(soup, source)
    row.update({k: v for k, v in page.items() if v})

    sources_used = []
    if page["ingredients"] or page["nutrition_raw"]:
        sources_used.append("page_text")
        row.update(parse_nutrients_from_text(
            " ".join([page["nutrition_raw"], page["ingredients"]])))

    if reader is not None:
        images = gallery_images(html, soup, source, max_images)
        if images:
            label = reader.read(fetcher, images)
            if label and label.get("label_found"):
                sources_used.append("label_image")
                row["label_image_url"] = label.get("label_image_url", "")
                # Label is the authoritative source — it overrides page text.
                if label.get("ingredients"):
                    row["ingredients"] = label["ingredients"]
                if label.get("nutrition_text"):
                    row["nutrition_raw"] = label["nutrition_text"]
                for key in NUTRIENT_KEYS:
                    if label.get(key):
                        row[key] = label[key]

    row["data_source"] = "+".join(sources_used) or "none"
    if not row["product_name"]:
        row["status"] = "parse error: product name not found (layout changed?)"
    elif not sources_used:
        row["status"] = "partial: no nutrition/ingredients found on page or label"
    else:
        missing = [f for f in ("ingredients", "nutrition_raw") if not row[f]]
        row["status"] = "ok" if not missing else f"partial: missing {', '.join(missing)}"
    return row


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export nutrition facts + ingredients from Amazon.in / Flipkart / Blinkit to CSV")
    parser.add_argument("url", nargs="*", help="Product URLs")
    parser.add_argument("--category", action="append", default=[],
                        help="Category/search listing URL to crawl (repeatable)")
    parser.add_argument("--urls", help="File with one product URL per line")
    parser.add_argument("--out", default="nutrition_export.csv")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--max-images", type=int, default=8)
    parser.add_argument("--model", default="claude-opus-4-8")
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--lat", type=float, default=12.9716)
    parser.add_argument("--lon", type=float, default=77.5946)
    args = parser.parse_args()

    urls = list(args.url)
    if args.urls:
        with open(args.urls, encoding="utf-8") as fh:
            urls += [l.strip() for l in fh
                     if l.strip() and not l.strip().startswith("#")]

    fetcher = Fetcher(args.lat, args.lon)
    for cat in args.category:
        urls += [u for u in crawl_category(fetcher, cat, args.max_items)
                 if u not in urls]
    if not urls:
        parser.error("No product URLs. Pass URLs, --urls FILE, or --category URL.")

    reader = None
    if not args.no_vision:
        try:
            reader = LabelReader(args.model)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Claude vision disabled ({exc}).")
            print("[warn] Set ANTHROPIC_API_KEY to read nutrition labels from images;"
                  " continuing with page text only.")

    rows = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        row = scrape_product(fetcher, reader, url, args.max_images)
        print(f"    -> {row['status']} (data: {row['data_source'] or 'n/a'})")
        rows.append(row)
        # Write incrementally so a crash mid-run doesn't lose everything.
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        if i < len(urls):
            time.sleep(args.delay + random.uniform(0, 3))

    ok = sum(1 for r in rows if r["status"].startswith(("ok", "partial")))
    print(f"\nWrote {len(rows)} rows ({ok} with data) to {args.out}")


if __name__ == "__main__":
    main()
