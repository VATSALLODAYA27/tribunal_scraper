"""Offline end-to-end check: fake sources, temp folder. No network, no cost. Run: python test_pipeline.py"""
import argparse, datetime as dt, json, shutil, tempfile, threading, types, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf

import review
import tribunal_scraper as ts

out = Path(tempfile.mkdtemp(prefix="tribunal_test_"))
try:
    # ---- a webhook catcher, to prove alerts are sent ----
    got = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    hook = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    ts.os.environ["ALERT_WEBHOOK_URL"] = f"http://127.0.0.1:{hook.server_port}/"

    # ---- fake NCLT PDF order (as IBBI would list it) ----
    body = ("IN THE NATIONAL COMPANY LAW TRIBUNAL\nKOLKATA BENCH\nC.P. (IB) No. 54/KB/2026\nIN THE MATTER OF:\nSTATE BANK OF INDIA\n"
            "...Financial Creditor\n-VERSUS-\nX STEELS PRIVATE LIMITED\n...Corporate Debtor\nCoram:\nShri. Labh Singh, Member (Judicial)\n"
            "Ms. Rekha Kantilal Shah, Member (Technical)\nAppearances:\nORDER\n" + "The petition under Section 7 of the Insolvency and Bankruptcy "
            "Code, 2016 relies on (2019) 4 SCC 17. PAN ABCDE1234F. " * 12 + "\nThe petition is admitted.")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(page.rect + (36, 36, -36, -36), body, fontsize=8)
    pdf = doc.tobytes()

    def fake_ibbi(http, a, b):
        yield {"schema_version": "1.0", "tribunal_code": "NCLT", "bench": None, "case_number": "54/KB/2026", "case_number_norm": None,
               "case_type": None, "order_date": "2026-11-03", "order_type": "final", "title": "X Steels", "parties": [], "members": [],
               "language": "en", "source": {"name": "ibbi_nclt", "page_url": "https://ibbi.gov.in/x", "pdf_url": "https://ibbi.gov.in/a.pdf"},
               "_pdf": ("GET", "https://ibbi.gov.in/a.pdf", None)}

    def empty(http, a, b):
        return iter(())

    ts.Polite.req = lambda self, method, url, **kw: types.SimpleNamespace(content=pdf)  # no network
    ts.SOURCES.update(t_ibbi=fake_ibbi, t_empty=empty)
    ts.SOURCE_TRIBUNAL.update(t_ibbi="NCLT", t_empty="NCLAT")
    args = argparse.Namespace(out=str(out), trial=False, trial_limit=10, sources=["t_ibbi", "t_empty"],
                              date_from=dt.date(2025, 6, 1), date_to=dt.date(2026, 11, 10))
    ts.run(args)

    idx = ts.load_index(out)
    assert len(idx) == 1, idx
    recs = {e["source"]: json.loads((out / e["json_path"]).read_text(encoding="utf-8")) for e in idx.values()}
    n = recs["ibbi_nclt"]
    assert n["case_number_norm"] == "CP(IB)/54/KB/2026" and n["bench"] == "Kolkata Bench", (n["case_number_norm"], n["bench"])
    assert [p["name"] for p in n["parties"]] == ["STATE BANK OF INDIA", "X STEELS PRIVATE LIMITED"]
    assert n["extra"] == {"ibc_section": "7", "corporate_debtor": "X STEELS PRIVATE LIMITED", "outcome": "admitted"}, n["extra"]
    assert n["citations"] == ["(2019) 4 SCC 17"] and "ABCDE1234F" not in n["full_text"]
    assert n["status"] == "active", n["flags"]
    print("save + enrich: OK")

    # second run: nothing new saved twice, zero-order alert for the empty source
    ts.run(args)
    runs = [json.loads(l) for l in (out / "runs.jsonl").read_text().splitlines()]
    assert runs[-2]["skipped"] == 1 and runs[-2]["inserted"] == 0, runs[-2]  # t_ibbi on the second run
    alerts = [json.loads(l)["message"] for l in (out / "alerts.jsonl").read_text().splitlines()]
    assert any("t_empty: 0 orders" in a for a in alerts) and any("t_empty: 0 orders" in g["text"] for g in got), (alerts, got)
    print("resume + alerts + webhook: OK")

    # ---- review screen: flag one order, list it, fix it through the form ----
    entry = next(e for e in idx.values() if e["source"] == "ibbi_nclt")
    rec = json.loads((out / entry["json_path"]).read_text(encoding="utf-8"))
    rec.update(status="flagged", flags=["fewer_than_2_parties"], parties=rec["parties"][:1])
    st = review.Store(out)
    st.save(entry, rec)
    review.Handler.store = st
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "Flagged orders: 1" in page and "fewer_than_2_parties (1)" in page
    assert "STATE BANK" in urllib.request.urlopen(f"{base}/edit?uid={rec['order_uid']}").read().decode()
    form = {"token": review.TOKEN, "uid": rec["order_uid"], "action": "save", "case_number": rec["case_number"], "case_number_norm": "",
            "bench": "Kolkata Bench", "order_date": "2026-11-03", "order_type": "final", "title": "",
            "parties": "applicant: STATE BANK OF INDIA\nrespondent: X STEELS PRIVATE LIMITED", "members": "Labh Singh | Judicial Member"}
    urllib.request.urlopen(base + "/", data=urllib.parse.urlencode(form).encode())  # POST, follows the redirect
    fixed = json.loads((out / entry["json_path"]).read_text(encoding="utf-8"))
    assert fixed["status"] == "active" and fixed["title"] == "STATE BANK OF INDIA v. X STEELS PRIVATE LIMITED", fixed["flags"]
    assert ts.load_index(out)[rec["order_uid"]]["status"] == "active"
    try:
        urllib.request.urlopen(base + "/", data=urllib.parse.urlencode({**form, "token": "wrong"}).encode())
        raise AssertionError("bad token accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    assert "Flagged orders: 0" in urllib.request.urlopen(base + "/").read().decode()
    print("review screen: OK")
    srv.shutdown(); hook.shutdown()
finally:
    shutil.rmtree(out, ignore_errors=True)
print("OK")
