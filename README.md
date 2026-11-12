# Tribunal order scraper

Collects Indian tribunal orders into one record format (see the "Tribunal Data Plan"):
NCLAT and NCLT (via IBBI) from the official sites for free, and ITAT, CESTAT, consumer commissions,
CAT, SAT, NGT and others from the paid Indian Kanoon API.

## Setup (Windows)

```
pip install requests beautifulsoup4 lxml pymupdf truststore pytesseract pillow anthropic
```

- Tesseract OCR installed at `C:\Program Files\Tesseract-OCR`.
- Hindi OCR: create `tessdata/` next to the script with `eng.traineddata` (copy from Tesseract's own `tessdata`)
  and `hin.traineddata` from https://github.com/tesseract-ocr/tessdata_fast. Without it, OCR is English only.

Optional settings (environment variables):

| Variable | Used for |
|---|---|
| `IK_API_TOKEN` | Indian Kanoon API sources (`ik_*`). Paid: about Rs 0.50 per search page + Rs 0.20 per document |
| `ANTHROPIC_API_KEY` | AI summaries with `--summarize` (or sign in with `ant auth login`) |
| `SUMMARY_MODEL` | Claude model for summaries, default `claude-opus-5-5` |
| `ALERT_WEBHOOK_URL` | Slack / Discord / Google Chat webhook for alerts |

## Run

```
python tribunal_scraper.py --trial
python tribunal_scraper.py --out "G:/My Drive/tribunals" --watch 6
python tribunal_scraper.py --out "G:/My Drive/tribunals" --sources ik_itat ik_cegat --from 2015-01-01 --summarize
python review.py --out "G:/My Drive/tribunals"
```

The review screen opens at http://127.0.0.1:8765 and lists flagged orders to fix, approve or hide.

## Tests (offline, free)

```
python test_ocr.py
python test_pipeline.py
python enrich.py
```

Indian Kanoon data must show their "powered by IKanoon" attribution (records carry `attribution_required: true`).
