# Nutrition Label Export Script (personal use)

`nutrition_label_export.py` exports **nutrition facts + ingredients** of food
products from **Amazon.in**, **Flipkart**, and **Blinkit** into a CSV.

Because most food products list their nutrition facts and ingredients on the
**back-of-pack label photo** rather than in the page text, the script pulls
the product gallery images and uses **Claude vision** to find the label image
and read the nutrition table + ingredients list off it. Page text (when
present) is used as a secondary source.

## Setup

```bash
cd scripts
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...   # from https://platform.claude.com
```

Playwright is required for Blinkit and for category crawling (both are
JavaScript-rendered), and strongly recommended for Amazon/Flipkart too.
The Anthropic API key is required for reading labels from images; without it
the script still runs but exports page text only.

## Usage

**Everything in a food category** (crawls the listing page, then scrapes each
product):

```bash
python nutrition_label_export.py \
    --category "https://www.amazon.in/s?k=breakfast+cereals" \
    --category "https://www.flipkart.com/food-nutrition/pr?sid=eat" \
    --category "https://blinkit.com/cn/munchies/cid/1237" \
    --max-items 50 --out nutrition.csv
```

**Specific products:**

```bash
python nutrition_label_export.py --urls urls.txt --out nutrition.csv
python nutrition_label_export.py "https://www.amazon.in/dp/B0XXXXXXXX"
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--max-items N` | Products collected per category (default 50) |
| `--max-images N` | Gallery images sent to Claude per product (default 8) |
| `--model ID` | Claude model (default `claude-opus-4-8`; `claude-haiku-4-5` is cheaper) |
| `--no-vision` | Skip label reading, page text only (no API key needed) |
| `--lat / --lon` | Delivery location for Blinkit (default Bengaluru) |
| `--delay SECS` | Base delay between products (default 5s + jitter) |

## Output columns

| Column | Description |
|---|---|
| `product_name`, `brand` | From the product page |
| `serving_size`, `energy_kcal`, `protein_g`, `carbohydrate_g`, `sugar_g`, `total_fat_g`, `saturated_fat_g`, `trans_fat_g`, `fiber_g`, `sodium_mg` | Read from the label image (preferred) or parsed from page text |
| `ingredients` | Ingredients list, transcribed from the label image or page text |
| `nutrition_raw` | Full nutrition table as plain text (nothing is lost if a value column is empty) |
| `label_image_url` | URL of the gallery image Claude identified as the label |
| `data_source` | `label_image`, `page_text`, `page_text+label_image`, or `none` |
| `status` | `ok`, `partial: …`, `blocked …`, or an error message |

The CSV is written incrementally after every product, so a crash or Ctrl-C
mid-run keeps everything scraped so far.

## Cost note

Each product with images costs one Claude vision call (~8 images). With the
default `claude-opus-4-8` that's roughly $0.05–0.15 per product; pass
`--model claude-haiku-4-5` to cut that ~5x if the labels are clearly printed.

## Notes & limitations

- Values come from whatever the seller uploaded — label photos can be low-res,
  outdated, or for a different pack size. Claude is instructed never to guess
  unreadable values, but spot-check anything important.
- Blinkit is a hyperlocal app: it only shows products deliverable to the
  `--lat/--lon` location, and its location handling changes often — treat
  Blinkit support as best-effort.
- The script is intentionally slow (single-threaded, ~5–8s between products).
  Keep it polite and use it for personal lists; scraping these sites at scale
  is against their terms of service. A 50-item category takes ~10–15 minutes.
- All three sites change their HTML frequently. If parsing breaks, the
  selectors live in `parse_page_text()` / `gallery_images()` /
  `product_links()`.
