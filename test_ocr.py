"""Offline check: a PDF whose only page is an image of text must come back as OCR text. Run: python test_ocr.py"""
import os
import pymupdf
from tribunal_scraper import OCR_LANG, detect_language, pdf_text

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

# Hindi scan (needs tessdata/hin.traineddata and the Windows Nirmala UI font)
FONTS = r"C:\Windows\Fonts"
if "hin" in OCR_LANG and os.path.exists(os.path.join(FONTS, "Nirmala.ttc")):
    HI = "राष्ट्रीय कंपनी विधि अधिकरण, नई दिल्ली पीठ। यह आवेदन दिवाला और शोधन अक्षमता संहिता के अंतर्गत दायर किया गया है।"
    hi = pymupdf.open()
    hi.new_page().insert_htmlbox(pymupdf.Rect(40, 60, 560, 400), f'<p style="font-family:Nirmala UI; font-size:16px">{HI}</p>',
                                 archive=pymupdf.Archive(FONTS), css='@font-face {font-family:"Nirmala UI"; src:url(Nirmala.ttc);}')
    hscan = pymupdf.open()
    hscan.new_page().insert_image(hscan[0].rect, stream=hi[0].get_pixmap(dpi=200).tobytes("png"))
    text, _, how = pdf_text(hscan.tobytes())
    words = ["राष्ट्रीय", "कंपनी", "अधिकरण", "दिल्ली", "आवेदन", "संहिता"]
    found = sum(w in text.split() for w in words)
    assert how == "ocr" and detect_language(text) == "hi" and found >= 5, (found, how)  # words must keep their spaces
    print(f"hindi page   -> ocr, {found}/6 words")
else:
    print("hindi page   -> skipped (no hin.traineddata or Nirmala UI font)")
print("OK")
