# Tribunal order scraper

Collects Indian tribunal orders from free, official sources into one record format (see the "Tribunal Data Plan"):
NCLAT judgments and NCLT insolvency orders (via IBBI's public list). No paid APIs, no CAPTCHA bypass.

## Setup (Windows)

```
pip install requests beautifulsoup4 lxml pymupdf truststore pytesseract pillow
```

- Tesseract OCR installed at `C:\Program Files\Tesseract-OCR`.
- Hindi OCR: create `tessdata/` next to the script with `eng.traineddata` (copy from Tesseract's own `tessdata`)
  and `hin.traineddata` from https://github.com/tesseract-ocr/tessdata_fast. Without it, OCR is English only.
- Optional: set `ALERT_WEBHOOK_URL` to a Slack / Discord / Google Chat webhook to get alerts there.

## Run

```
python tribunal_scraper.py --trial
python tribunal_scraper.py --out "G:/My Drive/tribunals" --watch 6
python review.py --out "G:/My Drive/tribunals"
```

The review screen opens at http://127.0.0.1:8765 and lists flagged orders to fix, approve or hide.

## Tests (offline)

```
python test_ocr.py
python test_pipeline.py
python enrich.py
```
