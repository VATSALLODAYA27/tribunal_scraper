"""
Tribunal order scraper (open sites only: no CAPTCHA, no login). Follows "Tribunal Data Plan" sections 4, 5, 7 and 8.

Sources:
  nclat      NCLAT judgments/orders, by order date   (nclat.nic.in/display-board/judge)
  ibbi_nclt  NCLT IBC orders mirrored by IBBI        (ibbi.gov.in/en/orders/nclt)

Skipped on purpose: NCLT site, ITAT, CESTAT, NGT (CAPTCHA on search),
APTEL (search needs a diary number, no date listing).

Output folder (--out), plan section 5 layout:
  raw/{TRIBUNAL}/{yyyy}/{mm}/{order_uid}.pdf    original file, never edited
  text/{TRIBUNAL}/{yyyy}/{mm}/{order_uid}.txt   cleaned + masked text
  json/{TRIBUNAL}/{yyyy}/{mm}/{order_uid}.json  the full record (schema_version 1.0)
  index.jsonl                                   one line per saved order (resume point + duplicate checks)
  runs.jsonl                                    one line per site per run (plan's scrape_runs)
Records that fail a check are still saved, with status "flagged" and the reasons in "flags" (the review queue).

Usage:
  python tribunal_scraper.py --trial                      # print what it finds, save nothing
  python tribunal_scraper.py --out "G:/My Drive/tribunals" --from 2026-10-01 --to 2026-11-04
  python tribunal_scraper.py --out "G:/My Drive/tribunals" --watch 6   # re-run every 6 h while the PC is on
"""
import argparse, datetime as dt, hashlib, json, os, re, sys, time, unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import truststore; truststore.inject_into_ssl()  # use Windows cert store (aptel/nic sites send incomplete chains)
import requests
from bs4 import BeautifulSoup
import pymupdf as fitz

SCHEMA_VERSION = "1.0"
UA = "LawgicHubBot/0.1 (tribunal order indexing)"  # add a work contact address here once you have one
DELAY = 3.0          # seconds between requests to the same site
MAX_FAILS = 5        # consecutive failures before we stop hitting a site
LOOKBACK_DAYS = 14   # sites upload orders ~a week late, so each run re-checks the last 2 weeks
TRIBUNAL_START = {"NCLT": dt.date(2016, 6, 1), "NCLAT": dt.date(2016, 6, 1)}  # both constituted 1 June 2016


class SiteDown(Exception):
    pass


class Polite:
    """One HTTP session per site: honest UA, robots.txt, fixed delay, backoff on errors, stop after repeated failures."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last = 0.0
        self.fails = 0
        self.robots = {}

    def allowed(self, url):
        host = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=30)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.ok:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True  # no robots.txt = no rules published
            except requests.RequestException:
                rp.allow_all = True
            self.last = time.time()
            self.robots[host] = rp
        return self.robots[host].can_fetch(UA, url)

    def req(self, method, url, **kw):
        if not self.allowed(url):
            raise SiteDown(f"robots.txt disallows {url}")
        while True:
            wait = self.last + DELAY - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.request(method, url, timeout=120, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                r.raise_for_status()
                self.fails = 0
                return r
            except requests.RequestException as e:
                self.fails += 1
                if self.fails >= MAX_FAILS or (e.response is not None and e.response.status_code in (403, 404)):
                    raise SiteDown(f"{url}: {e}") from e
                ra = e.response.headers.get("Retry-After") if e.response is not None else None
                backoff = int(ra) if ra and ra.isdigit() else min(30 * 2 ** (self.fails - 1), 600)
                print(f"  ! {e} -> waiting {backoff}s ({self.fails}/{MAX_FAILS})", file=sys.stderr)
                time.sleep(backoff)


# ---------------- field parsing (plan section 4) ----------------
def order_type(label):
    l = label.lower()
    return "interim" if "interim" in l else "final" if ("final" in l or "judg" in l) else "daily"


def norm_case(s):
    return re.sub(r"\s+", "", s.upper())


# TYPE [No.] [/] NUMBER then "/BENCH/" or "(BENCH)" or "/" or " of ", then YEAR
CASE = re.compile(r"([A-Za-z][A-Za-z.() &]*?)\s*(?:Nos?[.,]?)?\s*/?\s*(\d+)\s*"
                  r"(?:/\s*([A-Za-z]{1,6})\s*/|\(\s*([A-Za-z]{1,6})\s*\)|/|\s+of)\s*((?:19|20)\d\d)\b")


def case_norm(text):
    """'Company Appeal(AT)(Ins) No. 286/ND/2025' -> 'COMPANYAPPEAL(AT)(INS)/286/ND/2025'; None if unparsable."""
    m = CASE.search(text or "")
    if not m:
        return None
    typ = re.sub(r"[\s.]", "", m.group(1)).upper()  # full type name, not initials: "Company" and "Competition" Appeal must not collide
    return "/".join(x for x in (typ, m.group(2), (m.group(3) or m.group(4) or "").upper(), m.group(5)) if x)[:120]


VS = re.compile(r"\s+(?:vs\.?|v/s\.?|versus|v\.)\s+", re.I)


def parties_from(text, first, second):
    names = [n.strip(" ,.") for n in VS.split(text or "", maxsplit=1) if n.strip(" ,.")]
    if len(names) == 1:
        return [{"role": "other", "name": names[0]}]
    return [{"role": r, "name": n} for r, n in zip((first, second), names)]


def title_from(parties):
    return " v. ".join(p["name"] for p in parties[:2])


def members_from(coram):
    out = []
    for part in re.split(r"\s*,\s*(?=Hon)", coram or ""):  # one member per "Hon'ble ..."
        m = re.match(r"(?:Hon['’]?ble\s+)?(.*?)\s*\((.*)\)\s*$", part.strip())
        name, des = (m.group(1), m.group(2)) if m else (re.sub(r"^Hon['’]?ble\s+", "", part.strip()), None)
        if des:
            mm = re.fullmatch(r"Member\s*\(\s*(\w+)\s*\)", des)
            des = f"{mm.group(1).title()} Member" if mm else des
        if name:
            out.append({"name": name, "designation": des})
    return out


# ---------------- text cleanup + masking (plan section 5) ----------------
PAGE_NO = re.compile(r"^(page\s*)?[-–(]?\s*\d{1,4}\s*[-–)]?\s*(of\s*\d{1,4})?$", re.I)
MASKS = [  # order matters: account numbers before the phone rule can eat their digits
    (re.compile(r"(?i)\b((?:a/c|account)\s*(?:no\.?|number)?\s*[:.\-]?\s*)\d{9,18}\b"), r"\1[ACCOUNT]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b[2-9]\d{3} ?\d{4} ?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
]


def clean_pages(pages):
    """NFC, collapse spaces, drop page-number lines and headers/footers repeated on most pages (first copy kept)."""
    pages = [[re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in unicodedata.normalize("NFC", p).splitlines()] for p in pages]
    repeated = set()
    if len(pages) >= 3:
        counts = Counter(l for p in pages for l in set(p) if l)
        repeated = {l for l, c in counts.items() if c >= len(pages) / 2 and len(l) < 120}
    seen, out = set(), []
    for p in pages:
        keep = []
        for l in p:
            if PAGE_NO.match(l) or (l in repeated and l in seen):
                continue
            seen.add(l)
            keep.append(l)
        out.append("\n".join(keep))
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(out)).strip()


def mask(text):
    for rx, rep in MASKS:
        text = rx.sub(rep, text)
    return text


def readable_ratio(text):
    t = re.sub(r"\s", "", text)
    return sum(c.isalnum() or c in ".,;:()[]/-'\"&%₹@!?’“”" for c in t) / len(t) if t else 0.0


# ---------------- identity + checks ----------------
def order_uid(rec):
    parts = [rec["tribunal_code"], rec["case_number_norm"] or norm_case(rec["case_number"]), rec["order_date"], rec["order_type"]]
    if rec["source"]["name"] == "ibbi_nclt":
        parts.append(rec["source"]["pdf_url"])  # IBBI can list two orders of one case on one day; its PDF link keeps them apart
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def validate(rec):
    """Plan section 5 rules. Returns a list of reasons; empty = record is fine."""
    flags = [f"missing_{f}" for f in ("tribunal_code", "case_number", "order_date", "order_type", "title", "full_text", "text_method", "language") if not rec.get(f)]
    if rec.get("tribunal_code") not in TRIBUNAL_START:
        flags.append("unknown_tribunal")
    elif rec.get("order_date"):
        d = dt.date.fromisoformat(rec["order_date"])
        if d > dt.date.today() or d < TRIBUNAL_START[rec["tribunal_code"]]:
            flags.append("bad_order_date")
    if not rec.get("case_number_norm"):
        flags.append("case_number_unparsed")
    if len(rec.get("parties") or []) < 2:
        flags.append("fewer_than_2_parties")
    text = rec.get("full_text") or ""
    if rec.get("text_method") == "needs_ocr":
        flags.append("needs_ocr")
    elif len(text) < 300:
        flags.append("short_text")
    elif readable_ratio(text) < 0.7:
        flags.append("garbled_text")
    return flags


# ---------------- NCLAT ----------------
NCLAT = "https://nclat.nic.in/display-board/"


def nclat_orders(http, date_from, date_to):
    page = BeautifulSoup(http.req("GET", NCLAT + "judge").text, "lxml")
    form = page.find("form", id="FormId")
    if not form:
        raise SiteDown("NCLAT search form not found (page layout changed?)")
    data = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
    for bench in ("delhi", "chennai"):
        data.update(search_by="order_date_wise", location=bench, case_type="All", court="all",
                    from_date=date_from.strftime("%m/%d/%Y"),  # site expects month first
                    to_date=date_to.strftime("%m/%d/%Y"))
        r = http.req("POST", NCLAT + "judgement_details", data=data,
                     headers={"X-Requested-With": "XMLHttpRequest", "Referer": NCLAT + "judge"})
        if "json" in r.headers.get("content-type", ""):
            print(f"  nclat {bench}: site said {r.text[:200]}", file=sys.stderr)
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for sec in soup.select("section[id^=demo_]"):
            filing = sec["id"].removeprefix("demo_")
            try:
                head = soup.find(attrs={"data-filing_no": filing}).find_parent("tr")
                _, _, case_no, case_type, title, bench_name = [c.get_text(" ", strip=True) for c in head.find_all(["th", "td"])][:6]
            except (AttributeError, ValueError) as e:
                yield {"_error": f"nclat case {filing}: {e!r}"}
                continue
            parties = parties_from(title, "appellant", "respondent")
            case_number = f"{case_type} No. {case_no}"
            for tr in sec.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                try:
                    d = dt.datetime.strptime(tds[1].get_text(strip=True), "%m-%d-%Y").date()
                except ValueError as e:
                    yield {"_error": f"nclat case {filing}: {e}"}
                    continue
                if not (date_from <= d <= date_to):
                    continue  # case history also lists older orders of the same case
                f = tds[4].find("form")
                if not f:
                    yield {"_error": f"nclat case {filing} {d}: no download button"}
                    continue
                fields = {i["name"]: i.get("value", "") for i in f.find_all("input") if i.get("name")}
                yield {
                    "schema_version": SCHEMA_VERSION,
                    "tribunal_code": "NCLAT",
                    "bench": bench_name,
                    "case_number": case_number,
                    "case_number_norm": case_norm(case_number),
                    "case_type": case_type,
                    "order_date": d.isoformat(),
                    "order_type": order_type(tds[2].get_text(strip=True)),
                    "title": title_from(parties) or title,
                    "parties": parties,
                    "members": members_from(tds[3].get_text(" ", strip=True)),
                    "language": "en",
                    "source": {"name": "nclat_site", "page_url": NCLAT + "judge", "pdf_url": f["action"],
                               "filing_no": filing, "pdf_form": {k: v for k, v in fields.items() if k != "_token"}},
                    "_pdf": ("POST", f["action"], fields),
                }


# ---------------- IBBI mirror of NCLT ----------------
IBBI = "https://ibbi.gov.in/en/orders/nclt"


def ibbi_orders(http, date_from, date_to, max_pages=200):
    for p in range(1, max_pages + 1):
        soup = BeautifulSoup(http.req("GET", IBBI, params={"page": p}).text, "lxml")
        rows = [tr for tr in soup.select("table tr") if tr.find("td")]
        if not rows:
            return
        for tr in rows:
            tds = tr.find_all("td")
            try:
                d = dt.datetime.strptime(tds[1].get_text(strip=True), "%d %b, %Y").date()
            except (IndexError, ValueError) as e:
                yield {"_error": f"ibbi page {p}: {e!r}"}
                continue
            if d < date_from:
                return  # list is newest first
            if d > date_to:
                continue
            a = tr.find("a", href=re.compile(r"\.pdf", re.I))
            if not a:
                continue
            subject = tds[2].get_text(" ", strip=True)
            remarks = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""
            br = re.search(r"\[([^\]]+)\]", subject)  # case no. sits in brackets: "... [CP (IB) No. 54/KB/2026]"
            names = re.sub(r"\[.*", "", subject).strip(" ,.-")
            names = re.sub(r"^(in the matter of|in re:?)\s+", "", names, flags=re.I)
            if " - " in names:
                names = names.split(" - ", 1)[1]  # "Approval of Resolution Plan - X Ltd"
            parties = parties_from(names, "applicant", "respondent")
            pdf_url = urljoin(IBBI, a["href"])
            yield {
                "schema_version": SCHEMA_VERSION,
                "tribunal_code": "NCLT",
                "bench": None,  # bench name is inside the PDF, not in the list
                "case_number": br.group(1).strip() if br else subject,
                "case_number_norm": case_norm(br.group(1)) if br else None,
                "case_type": None,
                "order_date": d.isoformat(),
                "order_type": order_type(remarks) if "daily" in remarks.lower() else "final",
                "title": title_from(parties) or subject,
                "parties": parties,
                "members": [],  # only in the PDF
                "language": "en",
                "remarks": remarks,
                "source": {"name": "ibbi_nclt", "page_url": f"{IBBI}?page={p}", "pdf_url": pdf_url},
                "_pdf": ("GET", pdf_url, None),
            }


# ---------------- NCLT order header (parties, bench, members live only in the PDF) ----------------
NCLT_CITIES = {"new delhi": "New Delhi", "delhi": "New Delhi", "ahmedabad": "Ahmedabad", "allahabad": "Allahabad",
               "prayagraj": "Allahabad", "amaravati": "Amaravati", "bengaluru": "Bengaluru", "bangalore": "Bengaluru",
               "chandigarh": "Chandigarh", "chennai": "Chennai", "cuttack": "Cuttack", "guwahati": "Guwahati",
               "hyderabad": "Hyderabad", "indore": "Indore", "jaipur": "Jaipur", "kochi": "Kochi", "kolkata": "Kolkata",
               "mumbai": "Mumbai"}
BENCH_CODES = {"ND": "New Delhi", "PB": "New Delhi", "AHM": "Ahmedabad", "ALD": "Allahabad", "AMR": "Amaravati",
               "BB": "Bengaluru", "CHD": "Chandigarh", "CHE": "Chennai", "CB": "Cuttack", "GB": "Guwahati",
               "HYD": "Hyderabad", "IND": "Indore", "JPR": "Jaipur", "KOB": "Kochi", "KB": "Kolkata", "MB": "Mumbai"}
DESIG = re.compile(r"(?:Hon['’]?ble\s+)?(?:(Acting\s+)?President|Member\s*\(\s*(Judicial|Technical|J|T)\s*\))", re.I)
CORAM_END = re.compile(r"^(appear|for\s|counsel|order|o r d e r|in the national|national company|present|date|pronounced|\[?per\b|\(heard|item|coram|an application)", re.I)
MARKER = re.compile(r"(?:in\s*the\s*(?:original\s+)?matter\s*of|name\s+of\s+the\s+parties)\s*[:;]?\s*", re.I)
SEP = re.compile(r"^-*\s*(?:vs\.?|v/s\.?|versus|and)\s*-*$", re.I)  # "VS", "-VERSUS-", "And"
NAME_STOP = re.compile(r"(…|\.\.\.|\bhaving\b|represented|registered|\bregd\b|\baddress|\(cin|\bcin\b|\bat[:\-])", re.I)
ROLE_WORD = re.compile(r"(petitioner|applicant|appellant|intervenor|complainant|respondent|opposite party)", re.I)
ROLE_ALIAS = re.compile(r"(personal guarantor|guarantor|(?:financial|operational)\s+creditor|creditor|corporate debtor|debtor|resolution professional)", re.I)
NAME_END = re.compile(r"\b(limited|ltd\.?|llp|bank)\s*\.?$", re.I)


def members_from_coram(text):
    """Reads the CORAM block: one member per line, or a line of names followed by a line of designations."""
    m = re.search(r"\bcoram\b\s*:?", text[:6000], re.I)
    if not m:
        return []
    out, pending = [], []
    for line in text[m.end():m.end() + 600].splitlines()[:8]:
        line = line.strip()
        if not line:
            continue
        if CORAM_END.match(line):
            break
        des = [("Acting President" if a else "President") if not k else ("Judicial Member" if k[0] in "Jj" else "Technical Member")
               for a, k in DESIG.findall(line)]
        rest = re.sub(r"(?i)\bHon['’]?ble\b\.?", " ", DESIG.sub(" ", line))
        # titles (Shri, Ms. ...), commas and wide gaps separate names when two sit on one line
        names = [n.strip(" ,.;:") for n in re.split(r"(?i)\b(?:Shri|Sh|Smt|Ms|Mr|Mrs|Dr)\b\.?|\s{2,}|,", rest)]
        names = [n for n in names if len(n) > 2]
        if des and names:
            out += [{"name": n, "designation": d} for n, d in zip(names, des)]
        elif des:  # names were on earlier lines; designations follow in the same order
            while des and pending:
                out.append({"name": pending.pop(0), "designation": des.pop(0)})
        elif names:
            pending += names
    return out


def _party_name(lines):
    parts = []
    for line in lines[:8]:
        line = re.sub(r"^\d+\.\s*", "", line.strip())
        if not line or MARKER.fullmatch(line):
            continue
        cut = NAME_STOP.search(line)
        if cut:
            line = line[:cut.start()]
        role = ROLE_WORD.search(line) or ROLE_ALIAS.search(line)
        if role:
            line = line[:role.start()]
        if line.strip(" .,") and not re.search(r"\d", line):
            parts.append(line.strip(" ,"))
        if cut or role or re.search(r"\d", line) or NAME_END.search(line):
            break
    name = re.sub(r"\s+", " ", " ".join(parts)).strip(" ,.;:")
    return name[:150] or None


def _role(lines, default):
    blob = " ".join(lines)
    m = ROLE_WORD.search(blob)
    if m:
        return m.group(1).lower().replace(" ", "_")
    m = ROLE_ALIAS.search(blob)  # creditor / RP files the case, debtor / guarantor answers it
    if m:
        return "applicant" if re.search(r"creditor|professional", m.group(1), re.I) else "respondent"
    return default


def _party_lines(lines):
    """Lines of one party after the separator, up to the next separator, marker or CORAM/appearance line."""
    out = []
    for l in lines[:10]:
        if out and (SEP.match(l) or MARKER.search(l) or CORAM_END.match(l)):
            break
        out.append(l)
    return out


def parties_from_header(text):
    """Finds 'X ...Petitioner / VS / Y ...Respondent' in the order header. Returns [] if unsure."""
    lines = [l.strip() for l in text[:5000].splitlines()]
    for i, l in enumerate(lines):
        if not SEP.match(l):
            continue
        before = lines[:i]
        marks = [j for j, b in enumerate(before) if MARKER.search(b)]
        if not marks:
            if l.strip("- ").lower() == "and":
                continue  # a bare "And" without "In the matter of" usually joins two case numbers
            start = max(0, i - 8)
            block = before[start:]
        else:
            first = before[marks[-1]][MARKER.search(before[marks[-1]]).end():]
            block = ([first] if first else []) + before[marks[-1] + 1:]
        after = _party_lines(lines[i + 1:])
        a, b = _party_name(block), _party_name(after)
        if a and b:
            return [{"role": _role(block, "applicant"), "name": a}, {"role": _role(after, "respondent"), "name": b}]
    return []


def nclt_header(rec, text):
    """Fill bench, members, parties and (if the list lacked it) case number from the PDF header."""
    head = text[:600].lower()
    hits = [(m.start(), v) for k, v in NCLT_CITIES.items() for m in [re.search(rf"\b{k}\b", head)] if m]
    city = min(hits)[1] if hits else None  # first city in the header is the bench; later ones are addresses
    if not city and rec.get("case_number_norm"):
        city = BENCH_CODES.get((rec["case_number_norm"].split("/") + ["", "", ""])[2])
    rec["bench"] = rec.get("bench") or (f"{city} Bench" if city else None)
    rec["members"] = rec.get("members") or members_from_coram(text)
    parties = parties_from_header(text)
    if len(parties) >= 2:
        rec["parties"], rec["title"] = parties, title_from(parties)
    if not rec.get("case_number_norm"):
        m = CASE.search(text[:800])
        if m:
            rec["case_number"], rec["case_number_norm"] = m.group(0).strip(), case_norm(m.group(0))
    return rec


SOURCES = {"nclat": nclat_orders, "ibbi_nclt": ibbi_orders}
SOURCE_TRIBUNAL = {"nclat": "NCLAT", "ibbi_nclt": "NCLT"}


# ---------------- PDF text + OCR ----------------
TESSDATA = os.environ.get("TESSDATA_PREFIX") or r"C:\Program Files\Tesseract-OCR\tessdata"
OCR_MIN_CHARS = 50   # a page with less text than this is treated as a scan


def pdf_text(data):
    """Text layer first; OCR (Tesseract via PyMuPDF) only for pages that have no text. Returns cleaned text, pages, method."""
    parts, ocr_pages, failed = [], 0, False
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            t = page.get_text()
            if len(t.strip()) < OCR_MIN_CHARS and not failed:
                try:
                    t = page.get_text(textpage=page.get_textpage_ocr(language="eng", dpi=300, full=True, tessdata=TESSDATA))
                    ocr_pages += 1
                except Exception as e:  # Tesseract missing/broken: keep going, mark the order for a later re-OCR
                    print(f"  ! OCR failed: {e}", file=sys.stderr)
                    failed = True
            parts.append(t)
        method = "needs_ocr" if failed else ("ocr" if ocr_pages else "pdf_text")
        return clean_pages(parts), doc.page_count, method


# ---------------- run ----------------
def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def save_order(http, rec, req, out, index, by_text, by_file):
    method, url, form = req
    data = http.req(method, url, data=form).content
    if not data.startswith(b"%PDF"):
        raise ValueError("download is not a PDF")
    text, pages, how = pdf_text(data)
    if rec["tribunal_code"] == "NCLT":
        nclt_header(rec, text)  # before masking, so numbers in the header are intact
    text = mask(text)
    uid, sha = rec["order_uid"], hashlib.sha256(data).hexdigest()
    rel = f"{rec['tribunal_code']}/{rec['order_date'][:4]}/{rec['order_date'][5:7]}/{uid}"
    pdf_path = by_file.get(sha)  # same file already saved under another order: don't store it twice
    if not pdf_path:
        pdf_path = f"raw/{rel}.pdf"
        (out / pdf_path).parent.mkdir(parents=True, exist_ok=True)
        (out / pdf_path).write_bytes(data)
    chash = hashlib.sha256(text.encode()).hexdigest()
    rec.update(full_text=text, text_method=how, page_count=pages, content_hash=chash)
    rec["source"].update(fetched_at=now(), file_sha256=sha, pdf_path=pdf_path)
    flags = validate(rec)
    if len(text) >= 300 and chash in by_text:
        flags.append(f"same_text_as:{by_text[chash]}")  # plan: same text under another identity = case no. likely parsed wrong
    rec.update(status="flagged" if flags else "active", flags=flags)
    for folder, ext, body in (("text", "txt", text), ("json", "json", json.dumps(rec, ensure_ascii=False, indent=1))):
        p = out / folder / f"{rel}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    entry = {"order_uid": uid, "content_hash": chash, "file_sha256": sha, "pdf_path": pdf_path, "status": rec["status"]}
    with (out / "index.jsonl").open("a", encoding="utf-8") as fh:  # written last = commit point; a crash before this just redoes the order
        fh.write(json.dumps(entry) + "\n")
    index[uid] = entry
    by_text.setdefault(chash, uid)
    by_file.setdefault(sha, pdf_path)
    return rec


def run(args):
    out = Path(args.out) if args.out else None
    index = {}
    if out:
        out.mkdir(parents=True, exist_ok=True)
        if (out / "index.jsonl").exists():
            for line in (out / "index.jsonl").open(encoding="utf-8"):
                e = json.loads(line)
                index[e["order_uid"]] = e
    by_text = {e["content_hash"]: u for u, e in index.items()}
    by_file = {e["file_sha256"]: e["pdf_path"] for e in index.values()}
    run_type = "backfill" if (args.date_to - args.date_from).days > LOOKBACK_DAYS else "daily"

    for name in args.sources:
        http = Polite()
        log = {"tribunal_code": SOURCE_TRIBUNAL[name], "source_name": name, "run_type": run_type,
               "date_from": args.date_from.isoformat(), "date_to": args.date_to.isoformat(), "started_at": now(),
               "found": 0, "inserted": 0, "flagged": 0, "skipped": 0, "failed": 0, "error": None}
        print(f"== {name} {args.date_from} .. {args.date_to}")
        try:
            for rec in SOURCES[name](http, args.date_from, args.date_to):
                if "_error" in rec:
                    log["failed"] += 1
                    print(f"  ! skipped row: {rec['_error']}", file=sys.stderr)
                    continue
                log["found"] += 1
                rec["order_uid"] = order_uid(rec)
                req = rec.pop("_pdf")
                if args.trial:
                    print(f"  {rec['order_date']} {rec['order_type']:<7} {str(rec['case_number_norm'] or '?? ' + rec['case_number'])[:42]:<42} "
                          f"{len(rec['parties'])} parties  {rec['title'][:50]}")
                    if log["found"] == 1:  # prove download + text + checks once, in memory only
                        method, url, form = req
                        text, pages, how = pdf_text(http.req(method, url, data=form).content)
                        if rec["tribunal_code"] == "NCLT":
                            nclt_header(rec, text)
                        rec.update(full_text=mask(text), text_method=how)
                        print(f"     -> from PDF: {rec['case_number_norm']} | {rec['bench']} | {rec['title'][:70]} | "
                              f"{[m['name'] for m in rec['members']]}")
                        print(f"     -> PDF ok ({how}): {pages} pages, {len(text)} chars; members={len(rec['members'])}; "
                              f"flags={validate(rec) or 'none'}")
                    if log["found"] >= args.trial_limit:
                        break
                    continue
                if rec["order_uid"] in index:
                    log["skipped"] += 1
                    continue
                try:
                    rec = save_order(http, rec, req, out, index, by_text, by_file)
                except SiteDown:
                    raise
                except Exception as e:  # one broken order must not stop the run
                    log["failed"] += 1
                    print(f"  ! {rec['case_number']} {rec['order_date']}: {e!r}", file=sys.stderr)
                    continue
                log["inserted"] += 1
                log["flagged"] += rec["status"] == "flagged"
        except SiteDown as e:
            log["error"] = str(e)
            print(f"  !! stopped {name}: {e}", file=sys.stderr)
        log["finished_at"] = now()
        log["status"] = "failed" if log["error"] and not log["found"] else "partial" if (log["error"] or log["failed"]) else "ok"
        if not log["found"] and not log["error"]:
            print(f"  !! ALERT {name}: 0 orders in {args.date_from}..{args.date_to} - site may have changed", file=sys.stderr)
        if out:
            with (out / "runs.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(log) + "\n")
        print(f"   {name}: found {log['found']}, saved {log['inserted']} ({log['flagged']} flagged), "
              f"already had {log['skipped']}, failed {log['failed']}, status {log['status']}")


def main():
    ap = argparse.ArgumentParser()
    today = dt.date.today()
    ap.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, default=today - dt.timedelta(days=LOOKBACK_DAYS))
    ap.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, default=today)
    ap.add_argument("--sources", nargs="+", default=list(SOURCES), choices=list(SOURCES))
    ap.add_argument("--out", help="folder for PDFs, text, JSON and logs (e.g. your Google Drive folder)")
    ap.add_argument("--trial", action="store_true", help="print only, save nothing")
    ap.add_argument("--trial-limit", type=int, default=10)
    ap.add_argument("--watch", type=float, help="repeat every N hours (last LOOKBACK_DAYS days each time)")
    args = ap.parse_args()
    if not args.trial and not args.out:
        ap.error("give --out FOLDER, or use --trial")
    while True:
        run(args)
        if not args.watch:
            break
        time.sleep(args.watch * 3600)
        args.date_from, args.date_to = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS), dt.date.today()


def _selftest():
    assert order_type("Final Order /Judgement") == "final" and order_type("Daily Order") == "daily"
    assert order_type("Interim Order") == "interim"
    assert case_norm("Company Appeal(AT)(Ins) No. 286/ND/2025") == "COMPANYAPPEAL(AT)(INS)/286/ND/2025"
    assert case_norm("IA No. 6445/ND/2024") == "IA/6445/ND/2024"
    assert case_norm("CP (IB) No. 54/KB/2026") == "CP(IB)/54/KB/2026"
    assert case_norm("I.A (IB) No. 1/2025 in C.P. (IB) No. 54/KB/2024") == "IA(IB)/1/2025"
    assert case_norm("In the matter of X Ltd") is None
    # formats seen on IBBI, 3-6 Nov 2026
    assert case_norm("CP (IB)/6(KB)2026") == "CP(IB)/6/KB/2026"
    assert case_norm("C.P. (IB)/224/KB/2023") == "CP(IB)/224/KB/2023"
    assert case_norm("I.A.(IBC)/1683 of 2026 and I.A. (IBC) (Plan)/9 of 2026") == "IA(IBC)/1683/2026"
    assert case_norm("IA(LIQ.)/40/2024 in C.P. (IB)/1197(MB)2019") == "IA(LIQ)/40/2024"
    assert case_norm("IA(IBC)(PLAN)/73/MB/2026") == "IA(IBC)(PLAN)/73/MB/2026"
    assert parties_from("Parveen Gupta VS BANK OF INDIA", "appellant", "respondent") == [
        {"role": "appellant", "name": "Parveen Gupta"}, {"role": "respondent", "name": "BANK OF INDIA"}]
    assert parties_from("X Pvt Ltd", "applicant", "respondent") == [{"role": "other", "name": "X Pvt Ltd"}]
    assert members_from("Hon'ble Justice N Sesha Sayee (Member(Judicial)) , Hon'ble Mr. Barun Mitra (Member (Technical))") == [
        {"name": "Justice N Sesha Sayee", "designation": "Judicial Member"}, {"name": "Mr. Barun Mitra", "designation": "Technical Member"}]
    assert mask("PAN ABCDE1234F, Aadhaar 2345 6789 0123, ph 9876543210, A/c No. 123456789012, filing 9910101009462025") == \
        "PAN [PAN], Aadhaar [AADHAAR], ph [PHONE], A/c No. [ACCOUNT], filing 9910101009462025"
    pages = ["NCLAT\nCompany Appeal No. 1\nbody one\n1", "NCLAT\nCompany Appeal No. 1\nbody two\nPage 2 of 3", "NCLAT\nCompany Appeal No. 1\nbody three\n- 3 -"]
    assert clean_pages(pages) == "NCLAT\nCompany Appeal No. 1\nbody one\n\nbody two\n\nbody three"
    ib = lambda pdf, norm: {"tribunal_code": "NCLT", "case_number": "x", "case_number_norm": norm, "order_date": "2026-11-03",
                            "order_type": "final", "source": {"name": "ibbi_nclt", "pdf_url": pdf}}
    assert order_uid(ib("a.pdf", "CP(IB)/54/KB/2026")) != order_uid(ib("b.pdf", "CP(IB)/54/KB/2026"))
    assert order_uid(ib("a.pdf", None)) == order_uid(ib("a.pdf", None))
    # NCLT order headers, trimmed from real IBBI PDFs (Nov 2026)
    two_col = ("NATIONAL COMPANY LAW TRIBUNAL,\nMUMBAI BENCH COURT III\n\nCORAM:\nSH. HARIHARAN NEELAKANTA IYER Ms. LAKSHMI GURUNG\n"
               "Member (Technical) Member (Judicial)\n\nORDER SHEET OF THE HEARING ON 06.11.2026\n\n"
               "NAME OF THE PARTIES: Bank of Maharashtra\nVS\nTransport Solutions India Pvt Ltd\nAppearance\nFor Applicant: Adv. X\n")
    r = nclt_header({"case_number": "x", "case_number_norm": None, "bench": None, "members": [], "parties": [], "title": ""}, two_col)
    assert r["bench"] == "Mumbai Bench", r["bench"]
    assert r["members"] == [{"name": "HARIHARAN NEELAKANTA IYER", "designation": "Technical Member"},
                            {"name": "LAKSHMI GURUNG", "designation": "Judicial Member"}], r["members"]
    assert r["parties"] == [{"role": "applicant", "name": "Bank of Maharashtra"},
                            {"role": "respondent", "name": "Transport Solutions India Pvt Ltd"}], r["parties"]
    kolkata = ("IN THE NATIONAL COMPANY LAW TRIBUNAL\nKOLKATA BENCH, (COURT NO.-II)\nKOLKATA\nC.P. (I.B)/6(KB)2026\n\n"
               "IN THE MATTER OF:\nTHENDIYAKAVO KOYAKUTTY HASSAN\nhaving\nregistered\nat 13/3A, Menambedu, Padi, Chennai, Tamil Nadu\n"
               "… Petitioner\nAnd\nIN THE MATTER OF:\nHEAVY\nENGINEERING\nCORPORATION\nPRIVATE\nLIMITED\n(CIN\nNo. U27100JH1958GOI000630)\n"
               "… Respondent\n\nCORAM:\nShri. Labh Singh, Hon’ble Member (Judicial)\nMs. Rekha Kantilal Shah, Hon’ble Member (Technical)\n\n"
               "Counsel appeared through physically or virtually\n")
    r = nclt_header({"case_number": "771", "case_number_norm": None, "bench": None, "members": [], "parties": [], "title": ""}, kolkata)
    assert r["bench"] == "Kolkata Bench" and r["case_number_norm"] == "CP(IB)/6/KB/2026", (r["bench"], r["case_number_norm"])
    assert r["parties"] == [{"role": "petitioner", "name": "THENDIYAKAVO KOYAKUTTY HASSAN"},
                            {"role": "respondent", "name": "HEAVY ENGINEERING CORPORATION PRIVATE LIMITED"}], r["parties"]
    assert [m["name"] for m in r["members"]] == ["Labh Singh", "Rekha Kantilal Shah"]
    split = ("IN THE MATTER OF:\nSTATE BANK OF INDIA\n...Financial Creditor\n-VERSUS-\nRAJESH SONTHALIA\n...Personal Guarantor\n-AND-\n"
             "Coram:\nSh. Prabhat Kumar\n\nSh. Sushil Mahadeorao Kochey\nMember (Technical)\nMEMBER (J)\nAppearances:\n")
    assert parties_from_header(split) == [{"role": "applicant", "name": "STATE BANK OF INDIA"}, {"role": "respondent", "name": "RAJESH SONTHALIA"}]
    assert members_from_coram(split) == [{"name": "Prabhat Kumar", "designation": "Technical Member"},
                                         {"name": "Sushil Mahadeorao Kochey", "designation": "Judicial Member"}]
    assert parties_from_header("IA(IBC)/1683 of 2026\nAND\nIA(IBC)/100 of 2025\nORDER\n") == []  # "AND" between case numbers is not a party split
    ok = {"tribunal_code": "NCLAT", "case_number": "x", "case_number_norm": "IA/1/ND/2024", "order_date": "2026-11-03",
          "order_type": "final", "title": "A v. B", "parties": [1, 2], "full_text": "word " * 100, "text_method": "pdf_text", "language": "en"}
    assert validate(ok) == []
    assert validate({**ok, "order_date": "2099-01-01", "parties": [1], "full_text": "short"}) == ["bad_order_date", "fewer_than_2_parties", "short_text"]


if __name__ == "__main__":
    _selftest()
    main()
