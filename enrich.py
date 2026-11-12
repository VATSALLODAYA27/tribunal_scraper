"""
Enrich step (plan section 7): acts and sections, citations, tribunal-specific fields, AI summary.
Pure text rules except summarize(), which calls the Claude API (only when the scraper runs with --summarize).
"""
import os, re, sys

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
    return out


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
REVENUE = re.compile(r"\b(ITO|DCIT|ACIT|JCIT|Addl\.?\s*CIT|CIT|Pr\.?\s*CIT|PCIT|Income[- ]tax Officer|Commissioner of Income|Revenue)\b", re.I)


def _last_word(text, words):
    """Which of `words` appears last in the order's closing part (the operative order)."""
    tail = text[-3000:].lower()
    hits = [(tail.rfind(w), w) for w in words if w in tail]
    return max(hits)[1] if hits else None


def extra_fields(rec, text, provs):
    code, ex = rec["tribunal_code"], {}
    head = text[:4000]
    if code in ("NCLT", "NCLAT"):
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
    elif code == "ITAT":
        m = re.search(r"(?:A\.?\s*Y\.?|Assessment\s+Years?)\s*[:.\-]?\s*(\d{4})\s*[-–]\s*(\d{2,4})", head, re.I)
        if m:
            ex["assessment_year"] = f"{m.group(1)}-{m.group(2)[-2:]}"
        first = (rec.get("parties") or [{}])[0].get("name", "")
        if first:
            ex["appeal_by"] = "revenue" if REVENUE.search(first) else "assessee"
        it = [p["section"] for p in provs if "income" in p["act"].lower() and "tax" in p["act"].lower()]
        if it:
            ex["section_in_dispute"] = it[0]
    elif code == "CESTAT":
        m = re.search(r"Final\s+Order\s+No\.?\s*:?\s*([\w/\-.]+(?:\s*/\s*\d{4})?)", text, re.I)
        if m:
            ex["final_order_no"] = m.group(1).strip(" .")
        m = re.search(r"\b(Customs|Central Excise|Excise|Service Tax)\b", head, re.I)
        if m:
            ex["tax_type"] = {"central excise": "excise"}.get(m.group(1).lower(), m.group(1).lower())
        m = re.search(r"Order[- ]in[- ](?:Original|Appeal)\s+No\.?\s*:?\s*([\w/\-.]+)", head, re.I)
        if m:
            ex["order_in_appeal_no"] = m.group(1).strip(" .")
    elif code == "CONSUMER":
        src = (rec.get("bench") or "") + " " + head[:600]
        level = re.search(r"\b(National|State|District)\b", src, re.I)
        if level:
            ex["commission_level"] = level.group(1).lower()
        if rec.get("bench") and " - " in rec["bench"]:
            ex["state"] = rec["bench"].split(" - ", 1)[1]
    elif code == "NGT":
        m = re.search(r"(Principal Bench|(?:Southern|Western|Eastern|Central) Zon(?:e|al Bench))", head, re.I)
        if m:
            ex["zone"] = m.group(1).title()
        m = re.search(r"Original\s+Application\s+No\.?\s*([\d/]+(?:\s*of\s*\d{4})?)", head, re.I)
        if m:
            ex["original_application_no"] = m.group(1)
    return ex


def enrich(rec, text):
    """Adds provisions, citations and extra to the record in place."""
    provs = provisions(text)
    rec["provisions"] = provs
    rec["citations"] = citations(text)
    rec["extra"] = {**rec.get("extra", {}), **extra_fields(rec, text, provs)}
    return rec


# ---------------- AI summary (Claude API) ----------------
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-opus-5-5")
SUMMARY_SYSTEM = (
    "You write short case summaries of Indian tribunal orders for practising lawyers. "
    "Use only what the order says; never add facts, citations or outcomes it does not contain. "
    "Write 4 short labelled parts, about 150 words in total: Facts, Issue, Decision, Reasoning. "
    "Name the provisions the decision turns on. Plain text, no markdown headings. "
    "If the order is only a procedural or daily order (adjournment, listing, notice), say so in one sentence instead."
)
_client = None


def summarize(rec, client=None):
    """Our own summary of the order (never a publisher headnote). Returns None if the model declines."""
    global _client
    if client is None:
        import anthropic
        _client = _client or anthropic.Anthropic()  # key from ANTHROPIC_API_KEY or an `ant auth login` profile
        client = _client
    resp = client.beta.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},  # a summary is a simple task; low effort keeps cost and time down
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the model declines, Anthropic re-runs it on a suitable fallback model
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Tribunal: {rec['tribunal_code']}\nCase: {rec.get('case_number')}\nDate: {rec.get('order_date')}\n\n"
                   f"<order>\n{rec['full_text']}\n</order>"}],
    )
    if resp.stop_reason == "refusal":
        print(f"  ! summary declined for {rec.get('case_number')}", file=sys.stderr)
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or None


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
    assert citations(t) == ["(2019) 4 SCC 17", "AIR 1973 SC 1461", "[2020] 117 taxmann.com 23"], citations(t)
    nclt = {"tribunal_code": "NCLT", "parties": [{"role": "applicant", "name": "SBI"}, {"role": "respondent", "name": "X Ltd"}]}
    ex = extra_fields(nclt, t + " ... Hence the petition is admitted.", p)
    assert ex == {"ibc_section": "7", "corporate_debtor": "X Ltd", "outcome": "admitted"}, ex
    itat = {"tribunal_code": "ITAT", "parties": [{"role": "appellant", "name": "DCIT, Circle 1(1)"}, {"role": "respondent", "name": "A"}]}
    ex = extra_fields(itat, "ITA No. 1/Mum/2024 (A.Y. 2019-20) " + t, p)
    assert ex == {"assessment_year": "2019-20", "appeal_by": "revenue", "section_in_dispute": "68"}, ex


if __name__ == "__main__":
    _selftest()
    print("enrich selftest OK")
