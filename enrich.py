"""
Enrich step (plan section 7): acts and sections, citations, NCLT/NCLAT fields.
Pure text rules: no paid APIs.
"""
import re

# ---------------- acts and sections ----------------
IBC = "Insolvency and Bankruptcy Code, 2016"
CANON = [  # spelling variants -> one name, so search and section pages group correctly
    (r"insolvency\s*(?:and|&)\s*bankruptcy\s*code|^I\s*&\s*B\s*Code|^IB\s*Code|^I\.?B\.?C\.?$", IBC),
    (r"income[\s-]*tax\s+act", "Income-tax Act, 1961"),
    (r"^limitation\s+act", "Limitation Act, 1963"),
    (r"negotiable\s+instruments\s+act|^N\.?I\.?\s*Act", "Negotiable Instruments Act, 1881"),
    (r"^companies\s+act,?\s*2013", "Companies Act, 2013"),
    (r"^sarfaesi", "SARFAESI Act, 2002"),
    (r"^C\.?P\.?C\.?$|civil\s+procedure", "Code of Civil Procedure, 1908"),
    (r"^Cr\.?P\.?C\.?$|criminal\s+procedure", "Code of Criminal Procedure, 1973"),
]
SEC = r"(\d+[A-Z]?(?:\s*\(\s*\w{1,4}\s*\))*)"
KW = r"\b(?i:Sections?|Sec\.|u/s\.?)\s*"
FULL_ACT = re.compile(KW + SEC + r"((?:\s*(?:,|and|&|/)\s*" + SEC.replace("(", "(?:", 1) + r")*)"
                      r"\s+(?i:of\s+)?(?i:the\s+)?((?:[A-Z][\w\-'&,()]*\s+)(?:[\w\-'(),&]+\s+){0,8}?(?:Act|Code)(?:,?\s*\(?\d{4}\)?)?)")
SHORT_ACT = re.compile(KW + SEC + r"\s+(?i:of\s+)?(?i:the\s+)?(IBC|I\.B\.C|I\s*&\s*B\s*Code|IB\s*Code|CPC|IPC|CrPC|NI\s*Act|SARFAESI\s*Act)\b")
# every word is Capitalised or a small joining word: rejects "Read with Section 31 of the ...", "To (f) of the Code"
ACT_SHAPE = re.compile(r"^[A-Z][\w\-'&.,()]*(?:\s+(?:of|and|&|for|the|in|on|[A-Z][\w\-'&.,()]*))*\s+(?:Act|Code)(?:, \d{4})?$")


def _act_name(raw):
    name = re.sub(r"\s+", " ", raw).strip(" ,.")
    name = re.sub(r"^(?:the|and|or|of)\s+", "", name, flags=re.I)
    name = re.sub(r",?\s*\(?(\d{4})\)?$", r", \1", name)  # "Act 1961" / "Act, (1961)" -> "Act, 1961"
    for rx, canon in CANON:
        if re.search(rx, name, re.I):
            return canon
    words = re.sub(r", \d{4}$", "", name).split()
    if not ACT_SHAPE.match(name) or len(words) < 2 or len(words[0]) <= 2:  # "A of the Code" = "Section 29 A of the Code"
        return None  # not a clear act name ("the Code" alone doesn't say which code)
    return name


def provisions(text):
    """[{'act': 'Insolvency and Bankruptcy Code, 2016', 'section': '7'}, ...] in order of first mention."""
    out, seen = [], set()

    def add(act, sec):
        sec = re.sub(r"\s+", "", sec)
        key = (act.lower(), sec.lower())
        if act and sec and key not in seen and len(out) < 50:
            seen.add(key)
            out.append({"act": act, "section": sec})

    for m in FULL_ACT.finditer(text):
        act = _act_name(m.group(3))
        if act:
            for s in [m.group(1)] + re.findall(SEC, m.group(2) or ""):
                add(act, s)
    for m in SHORT_ACT.finditer(text):
        act = _act_name(m.group(2))
        if act:
            add(act, m.group(1))
    # "Indian Evidence Act" and "Indian Evidence Act, 1872" in one order are the same act: use the dated name
    dated = {re.sub(r", \d{4}$", "", p["act"]): p["act"] for p in out if re.search(r", \d{4}$", p["act"])}
    merged = []
    for p in out:
        p = {**p, "act": dated.get(p["act"], p["act"])}
        if p not in merged:
            merged.append(p)
    return merged


# ---------------- citations ----------------
CITATION_PATTERNS = [
    r"\(\d{4}\)\s*\d+\s*SCC\s*\d+",                         # (2019) 4 SCC 17
    r"\d{4}\s*SCC\s*OnLine\s*[A-Za-z]+\s*\d+",              # 2021 SCC OnLine SC 313
    r"AIR\s*\d{4}\s*(?:SC|[A-Z][a-z]+)\s*\d+",               # AIR 1973 SC 1461
    r"\[\d{4}\]\s*\d+\s*S\.?C\.?R\.?\s*\d+",                # [1950] 1 SCR 519
    r"\(\d{4}\)\s*\d+\s*ITR\s*\d+",                         # (2008) 299 ITR 1
    r"\[\d{4}\]\s*\d+\s*taxmann\.com\s*\d+",                # [2020] 117 taxmann.com 23
    r"\d{4}\s*\(\d+\)\s*E\.?L\.?T\.?\s*\d+",                # 2016 (340) ELT 3
]
CITATION = re.compile("|".join(f"(?:{p})" for p in CITATION_PATTERNS))


def citations(text):
    out = []
    for m in CITATION.finditer(text):
        c = re.sub(r"\s+", " ", m.group(0)).strip()
        if c not in out:
            out.append(c)
    return out[:200]


# ---------------- tribunal-specific fields (plan section 4, "extra") ----------------
def _last_word(text, words):
    """Which of `words` appears last in the order's closing part (the operative order)."""
    tail = text[-3000:].lower()
    hits = [(tail.rfind(w), w) for w in words if w in tail]
    return max(hits)[1] if hits else None


def extra_fields(rec, text, provs):
    ex = {}
    if rec["tribunal_code"] in ("NCLT", "NCLAT"):
        ibc = [re.match(r"\d+[A-Z]?", p["section"]).group(0) for p in provs if p["act"] == IBC]
        filing = [s for s in ibc if s in ("7", "9", "10", "94", "95", "54C")]  # sections a case is filed under beat definitions (s.5)
        if ibc:
            ex["ibc_section"] = (filing or ibc)[0]
        roles = {p["role"]: p["name"] for p in rec.get("parties", [])}
        sec = ex.get("ibc_section", "")
        if not (sec.isdigit() and 94 <= int(sec) <= 187):  # ss.94-187 are personal insolvency: no corporate debtor
            # shortcut: corporate debtor = side proceeded against (applicant for s.10 self-filing); read the "Corporate Debtor" label if this proves wrong
            debtor = roles.get("applicant") if sec == "10" else roles.get("respondent")
            if debtor:
                ex["corporate_debtor"] = debtor
        outcome = _last_word(text, ["admitted", "dismissed", "withdrawn", "allowed", "approved", "disposed of", "rejected"])
        if outcome:
            ex["outcome"] = outcome.replace(" of", "")
    return ex


def enrich(rec, text):
    """Adds provisions, citations and extra to the record in place."""
    provs = provisions(text)
    rec["provisions"] = provs
    rec["citations"] = citations(text)
    rec["extra"] = {**rec.get("extra", {}), **extra_fields(rec, text, provs)}
    return rec


def _selftest():
    t = ("This application is filed under Section 7 of the Insolvency and Bankruptcy Code, 2016 read with Rule 4. "
         "Sections 30(6) and 31 of the Insolvency and Bankruptcy Code, 2016 apply. Also u/s 9 of IBC. "
         "Addition under section 68 of the Income-tax Act, 1961 was made. See Section 7 of the Code. "
         "Relied on (2019) 4 SCC 17, AIR 1973 SC 1461, [2020] 117 taxmann.com 23 and (2019) 4 SCC 17 again.")
    p = provisions(t)
    assert {"act": "Insolvency and Bankruptcy Code, 2016", "section": "7"} in p, p
    assert {"act": "Insolvency and Bankruptcy Code, 2016", "section": "30(6)"} in p, p
    assert {"act": "Insolvency and Bankruptcy Code, 2016", "section": "31"} in p, p
    assert {"act": "Insolvency and Bankruptcy Code, 2016", "section": "9"} in p, p
    assert {"act": "Income-tax Act, 1961", "section": "68"} in p, p
    assert not any(x["act"].lower() == "code" for x in p)
    # noise seen in real NCLT orders
    noisy = ("Section 30(6) read with Section 31 of the Insolvency and Bankruptcy Code. Section 30(2)(a) to (f) of the Code. "
             "Sections 43, 45 or 66 of the Code. under Section 9 of The Insolvency & Bankruptcy Code, 2016 and Section 79(2) of the Income Tax Act, 1961.")
    assert provisions(noisy) == [{"act": IBC, "section": "31"}, {"act": IBC, "section": "9"},
                                 {"act": "Income-tax Act, 1961", "section": "79(2)"}], provisions(noisy)
    assert provisions("Section 65B of the Indian Evidence Act, 1872 and Section 65B of the Indian Evidence Act.") == \
        [{"act": "Indian Evidence Act, 1872", "section": "65B"}]
    assert citations(t) == ["(2019) 4 SCC 17", "AIR 1973 SC 1461", "[2020] 117 taxmann.com 23"], citations(t)
    nclt = {"tribunal_code": "NCLT", "parties": [{"role": "applicant", "name": "SBI"}, {"role": "respondent", "name": "X Ltd"}]}
    ex = extra_fields(nclt, t + " ... Hence the petition is admitted.", p)
    assert ex == {"ibc_section": "7", "corporate_debtor": "X Ltd", "outcome": "admitted"}, ex


if __name__ == "__main__":
    _selftest()
    print("enrich selftest OK")
