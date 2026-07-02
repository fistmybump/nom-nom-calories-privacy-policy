# Nutrition Export Script (personal use)

`export_nutrition.py` scrapes food product pages on **Amazon.in** and
**Flipkart** and exports the nutrition facts and ingredients to a CSV.

## Setup

```bash
cd scripts
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Put your product URLs in a file (see `urls.example.txt`), then:

```bash
python export_nutrition.py --urls urls.txt --out nutrition.csv
```

Or pass URLs directly:

```bash
python export_nutrition.py "https://www.amazon.in/dp/B0XXXXXXXX" --out nutrition.csv
```

### If you get blocked

Amazon in particular serves CAPTCHA pages to plain HTTP clients. If rows come
back with `blocked (CAPTCHA/robot check)`, install Playwright and use browser
rendering:

```bash
pip install playwright
playwright install chromium
python export_nutrition.py --urls urls.txt --playwright
```

## Output columns

| Column | Description |
|---|---|
| `product_name`, `brand` | From the product page |
| `serving_size` | Parsed from nutrition text when stated |
| `energy_kcal`, `protein_g`, `carbohydrate_g`, `sugar_g`, `total_fat_g`, `saturated_fat_g`, `trans_fat_g`, `fiber_g`, `sodium_mg` | Parsed numerically from the nutrition text when present |
| `ingredients` | Ingredients list as shown on the page |
| `nutrition_raw` | The full raw nutrition text (kept so nothing is lost if parsing misses a value) |
| `status` | `ok`, `partial: …`, `blocked …`, or an error message |

Rows are always written, even on failure, so you can retry just the failed
URLs.

## Notes

- Values come from whatever the seller listed on the page — they can be
  missing, stale, or formatted inconsistently. Spot-check against the physical
  label for anything important.
- The script is intentionally slow (~6–9 s between requests, single-threaded).
  Keep it polite and use it only for small personal lists; scraping these
  sites at scale is against their terms of service.
- Both sites change their HTML often. If parsing starts failing, the
  selectors in `parse_amazon()` / `parse_flipkart()` are the place to fix.
