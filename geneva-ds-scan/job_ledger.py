#!/usr/bin/env python3
"""
job_ledger.py — deterministic state engine for the Geneva DS/AI job scan.

Purpose
-------
Keep the LLM out of the business of *remembering* things. The model finds and
scores jobs; this script decides — by stable rules — which ones are genuinely
NEW, which were already e-mailed, and which you've already applied to / told it
to ignore. Dates and dedup are computed, never reasoned.

Single entry point used by the routine each run:

    python3 job_ledger.py ingest candidates.json [--email-threshold 55] [--commit]

  - Reads candidate jobs (a JSON list, schema below) that the model extracted.
  - Loads the persistent ledger (state/job_ledger.json).
  - Folds in applied.txt / ignore.txt (your hand-edited suppression lists).
  - De-duplicates within the batch and against history (cross-source).
  - Computes days-to-deadline and flags expired postings.
  - Prints, on stdout, the JSON list of postings to PUT IN THE EMAIL
    (new + not applied/ignored/expired + fit >= threshold), sorted by fit.
  - With --commit, writes the ledger back, marking those as emailed so they
    won't reappear next run. (Omit --commit for a dry run / preview.)

Helper commands (for you, ad hoc):

    python3 job_ledger.py applied <url-or-id>     # mark one applied right now
    python3 job_ledger.py ignore  <url-or-id>     # suppress one forever
    python3 job_ledger.py stats                   # funnel: seen/emailed/applied
    python3 job_ledger.py list [--status applied] # dump entries

Candidate JSON schema (list of objects). Only title+company are required;
everything else is best-effort and improves matching/output:

    {
      "title": "Senior Data Scientist",
      "company": "CERN",
      "location": "Geneva",
      "url": "https://jobs.smartrecruiters.com/CERN/744000117615032-...",
      "source": "smartrecruiters:CERN",
      "source_id": "TE-DPS-AIM-2026-76-GRAP",   # native ref if available
      "start_date": "2026-09-01",                # ISO if known, else null
      "closing_date": "2026-04-20",              # ISO if known, else null
      "fit_score": 92,                           # 0-100, set by the model
      "fit_reasons": "agentic AI + anomaly detection + PhD physics",
      "requirements": "PhD or MSc; PyTorch; English; French a plus; 100%"
    }

No third-party dependencies — Python 3.8+ stdlib only.
"""

import sys, os, json, re, hashlib, unicodedata, argparse
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
LEDGER_PATH = os.path.join(STATE_DIR, "job_ledger.json")
APPLIED_PATH = os.path.join(HERE, "applied.txt")
IGNORE_PATH = os.path.join(HERE, "ignore.txt")

# ---------------------------------------------------------------- normalisation

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))

def norm_text(s: str) -> str:
    """Lowercase, de-accent, drop workload %/punctuation, collapse whitespace."""
    if not s:
        return ""
    s = _strip_accents(str(s)).lower()
    s = re.sub(r"\(?\b\d{1,3}\s*[-–]\s*\d{1,3}\s*%\)?", " ", s)  # 80-100%
    s = re.sub(r"\b\d{1,3}\s*%", " ", s)                          # 100%
    s = re.sub(r"\b(h/?f|m/?f|m/?w/?d|w/m|f/h)\b", " ", s)        # gender tags
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def canon_url(u: str) -> str:
    """Canonical URL: scheme+host+path, lowercased host, no query/fragment/slash."""
    if not u:
        return ""
    u = u.strip()
    m = re.match(r"^(https?://)?([^/]+)(/[^?#]*)?", u, re.I)
    if not m:
        return u.lower()
    host = (m.group(2) or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (m.group(3) or "").rstrip("/")
    return f"{host}{path}"

def make_job_id(job: dict) -> str:
    """Stable id: native source id if present, else hash of company+title+loc."""
    src = norm_text(job.get("source", ""))
    sid = norm_text(job.get("source_id", ""))
    if sid:
        return f"{src or 'src'}:{sid}"
    basis = "::".join([norm_text(job.get("company", "")),
                       norm_text(job.get("title", "")),
                       norm_text(job.get("location", ""))])
    return "h:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

def dedup_key(job: dict) -> str:
    """Cross-source identity: same role on jobup + company site collapses here."""
    return "|".join([norm_text(job.get("company", "")),
                     norm_text(job.get("title", ""))])

# ---------------------------------------------------------------- dates

def parse_iso(d):
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(d).strip(), fmt).date()
        except ValueError:
            continue
    return None

def days_until(d):
    dd = parse_iso(d)
    return None if dd is None else (dd - date.today()).days

def deadline_label(d):
    n = days_until(d)
    if n is None:
        return ("", None)
    iso = parse_iso(d).isoformat()
    if n < 0:   return (f"closed {abs(n)} days ago ({iso})", n)
    if n == 0:  return (f"closes TODAY ({iso})", n)
    if n == 1:  return (f"closes TOMORROW ({iso})", n)
    if n <= 7:  return (f"closes in {n} days ({iso}) — urgent", n)
    return (f"closes in {n} days ({iso})", n)

# ---------------------------------------------------------------- persistence

def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {}
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_ledger(ledger):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LEDGER_PATH)

def read_suppress_list(path):
    """Lines of applied.txt / ignore.txt. '#'=comment. Accepts URL, id, or
    'company | title'. Returns (canon_urls:set, ids:set, pairs:set)."""
    urls, ids, pairs = set(), set(), set()
    if not os.path.exists(path):
        return urls, ids, pairs
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "|" in line and not line.lower().startswith("http"):
                c, _, t = line.partition("|")
                pairs.add(norm_text(c) + "|" + norm_text(t))
            elif line.lower().startswith("http"):
                urls.add(canon_url(line))
            else:
                ids.add(line.strip())
    return urls, ids, pairs

# ---------------------------------------------------------------- core: ingest

TERMINAL = {"applied", "ignored"}  # never re-email these
AGGREGATORS = ("jobup", "jobs", "linkedin", "indeed", "glassdoor")

def _is_aggregator(source: str) -> bool:
    s = (source or "").lower()
    return any(s.startswith(a) for a in AGGREGATORS)

def _merge_candidate(into: dict, job: dict):
    """Merge a duplicate candidate into a primary one. Prefer native ATS link,
    keep the highest fit, and backfill any missing field."""
    # fit: keep the max, and the reasons that came with it
    if (job.get("fit_score") or 0) > (into.get("fit_score") or 0):
        into["fit_score"] = job.get("fit_score")
        if job.get("fit_reasons"):
            into["fit_reasons"] = job.get("fit_reasons")
    # url/source: prefer a native ATS over an aggregator re-post
    if into.get("url") and job.get("url"):
        if _is_aggregator(into.get("source")) and not _is_aggregator(job.get("source")):
            into["url"], into["source"] = job["url"], job["source"]
            into["source_id"] = job.get("source_id") or into.get("source_id")
            into["title"] = job.get("title") or into["title"]  # cleaner native title
    # backfill blanks
    for k in ("location", "start_date", "closing_date", "requirements",
              "source_id", "url", "source", "title", "company"):
        if not into.get(k) and job.get(k):
            into[k] = job[k]

def ingest(candidates, email_threshold=55, commit=False):
    ledger = load_ledger()
    today = date.today().isoformat()
    appl_urls, appl_ids, appl_pairs = read_suppress_list(APPLIED_PATH)
    ign_urls, ign_ids, ign_pairs = read_suppress_list(IGNORE_PATH)

    def suppressed(jid, cu, dk):
        if jid in ign_ids or cu in ign_urls or dk in ign_pairs:
            return "ignored"
        if jid in appl_ids or cu in appl_urls or dk in appl_pairs:
            return "applied"
        return None

    # 1) Re-apply hand-edited suppression to existing ledger entries each run.
    for jid, e in ledger.items():
        st = suppressed(jid, e.get("canonical_url", ""), dedup_key(e))
        if st:
            e["status"] = st

    # 2) Collapse this run's candidates by cross-source identity FIRST.
    merged = {}            # job_id -> merged candidate dict
    dk_to_jid = {}         # dedup_key -> chosen job_id
    for job in candidates:
        jid = make_job_id(job)
        dk = dedup_key(job)
        if dk in dk_to_jid:               # already seen this role this run
            _merge_candidate(merged[dk_to_jid[dk]], job)
            continue
        dk_to_jid[dk] = jid
        merged[jid] = dict(job)

    # 3) Fold merged candidates into the ledger (one pass per unique role).
    to_email = []
    for jid, job in merged.items():
        cu = canon_url(job.get("url", ""))
        dk = dedup_key(job)
        label, n = deadline_label(job.get("closing_date"))
        expired = (n is not None and n < 0)

        entry = ledger.get(jid, {"job_id": jid, "first_seen": today,
                                 "status": "new", "emailed_runs": []})
        entry.update({
            "title": job.get("title", entry.get("title", "")),
            "company": job.get("company", entry.get("company", "")),
            "location": job.get("location", entry.get("location", "")),
            "url": job.get("url", entry.get("url", "")),
            "canonical_url": cu or entry.get("canonical_url", ""),
            "source": job.get("source", entry.get("source", "")),
            "source_id": job.get("source_id", entry.get("source_id", "")),
            "fit_score": job.get("fit_score", entry.get("fit_score", 0)),
            "fit_reasons": job.get("fit_reasons", entry.get("fit_reasons", "")),
            "requirements": job.get("requirements", entry.get("requirements", "")),
            "start_date": job.get("start_date", entry.get("start_date")),
            "closing_date": job.get("closing_date", entry.get("closing_date")),
            "deadline_label": label, "days_to_close": n, "last_seen": today,
        })

        st = suppressed(jid, cu, dk)
        if st:
            entry["status"] = st
        elif expired and entry["status"] not in TERMINAL:
            entry["status"] = "expired"

        ledger[jid] = entry

        fit = entry.get("fit_score") or 0
        if (entry["status"] == "new" and not entry.get("emailed_runs")
                and not expired and fit >= email_threshold):
            to_email.append(entry)

    # 4) Sort: fit desc, then soonest deadline first.
    def sort_key(e):
        n = e.get("days_to_close")
        return (-(e.get("fit_score") or 0), 10**6 if n is None else n)
    to_email.sort(key=sort_key)

    # 5) Commit (mark emailed) only when asked.
    if commit:
        for e in to_email:
            e["status"] = "emailed"
            e.setdefault("emailed_runs", []).append(today)
        save_ledger(ledger)

    return to_email

# ---------------------------------------------------------------- helper cmds

def _match_and_set(token, new_status):
    ledger = load_ledger()
    tok_url = canon_url(token) if token.lower().startswith("http") else None
    hits = 0
    for jid, e in ledger.items():
        if jid == token or (tok_url and e.get("canonical_url") == tok_url):
            e["status"] = new_status
            hits += 1
    save_ledger(ledger)
    # also persist to the suppression file so it survives a ledger reset
    path = APPLIED_PATH if new_status == "applied" else IGNORE_PATH
    with open(path, "a", encoding="utf-8") as f:
        f.write(token.strip() + "\n")
    print(f"{new_status}: matched {hits} ledger entr{'y' if hits==1 else 'ies'};"
          f" appended to {os.path.basename(path)}")

def stats():
    ledger = load_ledger()
    by = {}
    for e in ledger.values():
        by[e.get("status", "?")] = by.get(e.get("status", "?"), 0) + 1
    total = len(ledger)
    print(f"Total tracked: {total}")
    for k in sorted(by):
        print(f"  {k:9} {by[k]}")
    applied = by.get("applied", 0)
    emailed = by.get("emailed", 0) + applied
    if emailed:
        print(f"  apply-rate: {applied}/{emailed} surfaced = "
              f"{100*applied/emailed:.0f}%")

def list_entries(status=None):
    ledger = load_ledger()
    rows = [e for e in ledger.values()
            if status is None or e.get("status") == status]
    rows.sort(key=lambda e: (-(e.get("fit_score") or 0),
                             e.get("company", "")))
    for e in rows:
        print(f"[{e.get('status','?'):8}] {e.get('fit_score',0):3}  "
              f"{e.get('company','')} — {e.get('title','')}  "
              f"<{e.get('url','')}>")
    print(f"\n{len(rows)} entr{'y' if len(rows)==1 else 'ies'}")

# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description="Geneva DS job ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest")
    pi.add_argument("candidates")
    pi.add_argument("--email-threshold", type=int, default=55)
    pi.add_argument("--commit", action="store_true")

    pa = sub.add_parser("applied"); pa.add_argument("token")
    pg = sub.add_parser("ignore");  pg.add_argument("token")
    sub.add_parser("stats")
    pl = sub.add_parser("list"); pl.add_argument("--status", default=None)

    a = p.parse_args()
    if a.cmd == "ingest":
        with open(a.candidates, encoding="utf-8") as f:
            cands = json.load(f)
        out = ingest(cands, a.email_threshold, a.commit)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif a.cmd == "applied":
        _match_and_set(a.token, "applied")
    elif a.cmd == "ignore":
        _match_and_set(a.token, "ignored")
    elif a.cmd == "stats":
        stats()
    elif a.cmd == "list":
        list_entries(a.status)

if __name__ == "__main__":
    main()
