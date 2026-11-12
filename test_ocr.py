"""Offline check: a PDF whose only page is an image of text must come back as OCR text. Run: python test_ocr.py"""
import pymupdf
from tribunal_scraper import pdf_text

LINE = "IN THE NATIONAL COMPANY LAW APPELLATE TRIBUNAL PRINCIPAL BENCH NEW DELHI"

src = pymupdf.open()
src.new_page().insert_text((40, 100), LINE, fontsize=11)
png = src[0].get_pixmap(dpi=200).tobytes("png")  # flatten to a picture = a "scanned" page

scan = pymupdf.open()
scan.new_page().insert_image(scan[0].rect, stream=png)
text_layer_pdf = src.tobytes()

text, pages, how = pdf_text(scan.tobytes())
assert pages == 1 and "COMPANY LAW APPELLATE" in text.upper(), text
assert how == "ocr", how
print("scanned page ->", how, repr(text.strip()[:80]))

text, _, how = pdf_text(text_layer_pdf)
assert "APPELLATE" in text and how == "pdf_text", how  # normal PDFs must not be OCRed
print("text page    ->", how)
print("OK")
