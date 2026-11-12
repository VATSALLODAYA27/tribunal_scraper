"""
Review screen for flagged orders (plan section 7). Local only.

  python review.py --out "G:/My Drive/tribunals"      then open http://127.0.0.1:8765

Lists every order with status "flagged", grouped counts by reason, and lets you fix the fields,
then Save (re-checks the record), Approve (marks it active as-is) or Hide.
"""
import argparse, html, json, secrets, urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tribunal_scraper as ts

TOKEN = secrets.token_urlsafe(16)  # form token: stops other websites from posting to this local server
ROLES = ["appellant", "respondent", "petitioner", "applicant", "complainant", "opposite_party", "intervenor", "other"]
E = html.escape
STYLE = """<style>body{font:14px system-ui,sans-serif;margin:24px;max-width:1200px;color:#12161f}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #dde1e8;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eef2fa}a{color:#1f4fa8}.flag{display:inline-block;background:#fbf0d9;color:#7a5000;border-radius:4px;padding:1px 6px;margin:1px;font-size:12px}
label{display:block;margin-top:10px;font-weight:600}input[type=text],textarea,select{width:100%;padding:6px;font:13px ui-monospace,monospace;box-sizing:border-box}
button{margin:14px 8px 0 0;padding:8px 14px;border:0;border-radius:6px;background:#1f4fa8;color:#fff;cursor:pointer}
button.alt{background:#7a8193}pre{white-space:pre-wrap;background:#f4f6fa;padding:10px;max-height:420px;overflow:auto}</style>"""


class Store:
    def __init__(self, out):
        self.out = Path(out)

    def index(self):
        return ts.load_index(self.out)

    def load(self, uid):
        e = self.index().get(uid)
        if not e or "json_path" not in e:
            return None, None
        return e, json.loads((self.out / e["json_path"]).read_text(encoding="utf-8"))

    def save(self, entry, rec):
        (self.out / entry["json_path"]).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        with (self.out / "index.jsonl").open("a", encoding="utf-8") as fh:  # last line per order wins in load_index
            fh.write(json.dumps({"order_uid": rec["order_uid"], "status": rec["status"]}) + "\n")


def apply_form(rec, form, action):
    """Applies the edit form to the record and returns it (pure: easy to test)."""
    f = lambda k: (form.get(k) or [""])[0].strip()
    for k in ("case_number", "bench", "case_type", "order_type", "order_date", "title"):
        rec[k] = f(k) or None
    rec["case_number_norm"] = f("case_number_norm") or ts.case_norm(rec["case_number"] or "")
    parties = []
    for line in f("parties").splitlines():
        role, _, name = line.partition(":")
        role = role.strip().lower().replace(" ", "_")
        if name.strip():
            parties.append({"role": role if role in ROLES else "other", "name": name.strip()})
    rec["parties"] = parties
    rec["members"] = [{"name": n.strip(), "designation": (d.strip() or None)}
                      for n, _, d in (l.partition("|") for l in f("members").splitlines()) if n.strip()]
    rec["title"] = rec["title"] or ts.title_from(parties)
    if action == "approve":
        rec["status"], rec["flags"] = "active", []
    elif action == "hide":
        rec["status"] = "hidden"
    else:
        rec["flags"] = ts.validate(rec)
        rec["status"] = "flagged" if rec["flags"] else "active"
    rec["reviewed_at"] = ts.now()
    return rec


class Handler(BaseHTTPRequestHandler):
    store: Store = None

    def send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        uid = (q.get("uid") or [""])[0]
        if url.path == "/":
            return self.send(self.page_list((q.get("flag") or [""])[0]))
        if url.path == "/edit":
            entry, rec = self.store.load(uid)
            return self.send(self.page_edit(rec)) if rec else self.send("not found", code=404)
        if url.path == "/raw":
            entry, rec = self.store.load(uid)
            if not rec:
                return self.send("not found", code=404)
            path = self.store.out / entry["raw_path"]  # path comes from our own index, never from the URL
            ctype = "application/pdf" if path.suffix == ".pdf" else "text/html; charset=utf-8"
            return self.send(path.read_bytes(), ctype, extra={"Content-Security-Policy": "sandbox"})  # no scripts from saved pages
        self.send("not found", code=404)

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        if (form.get("token") or [""])[0] != TOKEN:
            return self.send("bad form token - reload the page", code=403)
        uid = (form.get("uid") or [""])[0]
        entry, rec = self.store.load(uid)
        if not rec:
            return self.send("not found", code=404)
        self.store.save(entry, apply_form(rec, form, (form.get("action") or ["save"])[0]))
        self.send("", code=303, extra={"Location": "/"})

    def page_list(self, only):
        rows, reasons = [], Counter()
        for uid, e in self.store.index().items():
            if e.get("status") != "flagged" or "json_path" not in e:
                continue
            rec = json.loads((self.store.out / e["json_path"]).read_text(encoding="utf-8"))
            flags = [f.split(":")[0] for f in rec.get("flags", [])]
            reasons.update(flags)
            if not only or only in flags:
                rows.append((rec["order_date"], rec, flags))
        rows.sort(key=lambda r: r[0], reverse=True)
        chips = " ".join(f'<a class="flag" href="/?flag={E(r)}">{E(r)} ({n})</a>' for r, n in reasons.most_common())
        body = "".join(
            f"<tr><td>{E(r['order_date'])}</td><td>{E(r['tribunal_code'])}</td><td>{E(r.get('case_number_norm') or r.get('case_number') or '')}</td>"
            f"<td>{E((r.get('title') or '')[:90])}</td><td>{''.join(f'<span class=flag>{E(f)}</span>' for f in fl)}</td>"
            f"<td><a href='/edit?uid={E(r['order_uid'])}'>review</a></td></tr>" for _, r, fl in rows[:500])
        return (f"<!doctype html><title>Review ({len(rows)})</title>{STYLE}<h1>Flagged orders: {len(rows)}</h1>"
                f"<p>By reason (click to filter): {chips or 'none'} <a href='/'>all</a></p>"
                f"<table><tr><th>Date</th><th>Tribunal</th><th>Case no.</th><th>Title</th><th>Flags</th><th></th></tr>{body}</table>")

    def page_edit(self, r):
        field = lambda k, label: f"<label>{label}</label><input type=text name={k} value='{E(str(r.get(k) or ''))}'>"
        parties = "\n".join(f"{p['role']}: {p['name']}" for p in r.get("parties", []))
        members = "\n".join(f"{m['name']} | {m.get('designation') or ''}" for m in r.get("members", []))
        otypes = "".join(f"<option {'selected' if r.get('order_type') == o else ''}>{o}</option>" for o in ("final", "interim", "daily"))
        return (f"<!doctype html><title>Review {E(r.get('case_number') or '')}</title>{STYLE}<p><a href='/'>&larr; back</a></p>"
                f"<h2>{E(r['tribunal_code'])} · {E(r['order_date'])}</h2>"
                f"<p>Flags: {''.join(f'<span class=flag>{E(f)}</span>' for f in r.get('flags', []))} · "
                f"<a href='/raw?uid={E(r['order_uid'])}' target=_blank>open original</a> · <a href='{E(r['source']['page_url'])}' target=_blank>source page</a></p>"
                f"<form method=post><input type=hidden name=token value='{TOKEN}'><input type=hidden name=uid value='{E(r['order_uid'])}'>"
                f"{field('case_number', 'Case number (as printed)')}{field('case_number_norm', 'Normalised case no. (blank = rebuild from case number)')}"
                f"{field('case_type', 'Case type')}{field('bench', 'Bench')}{field('order_date', 'Order date (YYYY-MM-DD)')}"
                f"<label>Order type</label><select name=order_type>{otypes}</select>{field('title', 'Title (blank = from parties)')}"
                f"<label>Parties: one per line, role: name (roles: {', '.join(ROLES)})</label><textarea name=parties rows=4>{E(parties)}</textarea>"
                f"<label>Members: one per line, name | designation</label><textarea name=members rows=3>{E(members)}</textarea>"
                f"<button name=action value=save>Save &amp; re-check</button><button name=action value=approve>Approve as is</button>"
                f"<button class=alt name=action value=hide>Hide</button></form>"
                f"<h3>Order text (start)</h3><pre>{E((r.get('full_text') or '')[:4000])}</pre>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="the scraper's output folder")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    Handler.store = Store(a.out)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)  # localhost only
    print(f"Review screen: http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
