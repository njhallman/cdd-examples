#!/usr/bin/env python3
"""Fetch every public float a company has reported to the SEC, for the homepage charts.

Writes
  data/site/public_float.csv      cik, as_of, value_usd, accn, form, filed
                                  one row per company and as-of date
  data/site/sec_entity_names.csv  cik, entity_name (the SEC's name for the company,
                                  used only to case company names for display)
  data/site/public_float_dropped.csv  floats dropped as scale errors (see Rules)

Reads
  data/site/public_float_exclusions.csv  the manual list of known misreported floats (see Rules)

Public float is the market value of the shares held by non-affiliates that a
company states on the cover of its annual report (XBRL tag dei:EntityPublicFloat,
dated as of the last day of its second fiscal quarter). This script downloads
every such value the SEC holds for each company, one request per company:

  https://data.sec.gov/api/xbrl/companyconcept/CIK##########/dei/EntityPublicFloat.json

Companies. Every CIK with an annual-meeting proxy (meeting_type == "annual")
filed in FROM_YEAR or later in the public raw file, or the CIKs listed in a file
(--ciks, one per line).

Rules
  * Only values in US dollars are kept.
  * Values that are zero or negative are dropped (shell companies often report 0).
  * Values above $10 trillion are dropped: no company has ever been worth that,
    so such a value can only be a units or typing error.
  * Scale errors. Some companies tag a float stated in thousands as dollars, so
    the value is 1,000 times too large ($4 trillion for a $4 billion company).
    For every company with any value of $1 billion or more, the script also
    downloads dei:EntityCommonStockSharesOutstanding (the share count on the
    cover) and drops a float that is more than $10,000 per share outstanding on
    the same filing (or, if that filing has no count, the count dated nearest
    the float's as-of date, within 400 days). Float counts only shares held outside the
    company, so float per share is always below the share price, and no
    company's shares have traded above $10,000 except Berkshire Hathaway's class
    A shares (see the README). Dropped values are listed in
    data/site/public_float_dropped.csv. These thresholds are fixed round numbers,
    not tuned to any result; a scale error in a company below $1 billion, or one
    whose true float per share is under $10, is not caught.
  * When the SEC holds several values for one company and as-of date (a report
    and its amendment, or the same cover repeated in later filings), the one
    filed last is kept; ties go to the larger accession number, then the larger
    value.
  * Out of line with the company's other years. The share-count check above
    misses scale errors at companies whose share price is low. So, after the
    rules above (one value per as-of date), a value is dropped when it is more
    than 50 times (MAX_JUMP) the company's typical float, the median of all its
    OTHER values, and is not reached gradually: the company's previous kept
    value (the latest earlier as-of date not dropped) is more than 50 times
    smaller, or there is none. The second condition keeps real growth: NVIDIA's
    2023-2025 floats ($1.1-4 trillion) are 60 to 200 times its median since 2008
    but each is within a few times the year before. The rule needs at least two
    other values; a company with one or two floats in all is not checked (TKO
    Group 2024). It is one pass: the median is not recomputed after a drop
    (recomputing added 7 drops, mostly real jumps such as CleanSpark 2021). It
    does not catch a company whose floats are mostly or all scale errors
    (Mister Car Wash, Nuvation Bio, Ponce Financial), because then the median is
    itself wrong. Dropped values are listed with the reason in
    data/site/public_float_dropped.csv.
    Values far BELOW the typical float are not dropped: the same rule mirrored
    would drop many real first floats of fast-growing companies (Paycom 2014,
    MongoDB 2018) and real collapses.
  * Manual list. After the rules above, the floats listed in
    data/site/public_float_exclusions.csv (cik, company, as_of, reason; as_of is
    a date or "all") are dropped and logged with the reason "known misreported
    (manual list)". It covers companies the rules cannot catch because most or
    all of their floats are scale errors, or they have too few floats (TKO
    Group 2024, Mister Car Wash, Nuvation Bio, Ponce Financial and a few dozen
    more values found by listing the largest floats used and the floats far
    above a company's own median). Each entry was checked by hand against the
    company's other years; only values about 1,000 times too large are listed.
    An entry that matches no row is reported on stderr.
A company with no public-float facts at all (the SEC answers 404) has no rows.

Run (stdlib only, Python 3.9+; about 35 minutes for 16,000 companies at the
SEC's limit of 8 requests a second; safe to stop and restart):

  python3 scripts/site/fetch_public_float.py --raw ~/Downloads/cdd_raw_v0.6.0/raw_extractions.jsonl.gz

Each response is kept under --cache (git-ignored), so a rerun downloads only
what is missing; --refresh downloads again. The output is sorted and does not
depend on download order, so the same responses give byte-identical files.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The same User-Agent as scripts/membership/size_ranks.py (SEC fair-access policy).
USER_AGENT = "Corporate Disclosure Data data@corporatedisclosuredata.org"
URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{:010d}/dei/{}.json"
FLOAT, SHARES = "EntityPublicFloat", "EntityCommonStockSharesOutstanding"
FROM_YEAR = 2010
MAX_FLOAT = 10e12          # $10 trillion
MAX_PRICE = 10_000         # dollars of float per share outstanding; above it the float is a scale error
CHECK_FROM = 1e9           # companies with any float this large are checked against shares outstanding
MAX_JUMP = 50              # times the company's typical float (median of its other values); see out_of_line()
MAX_RATE = 8.0             # requests per second; the SEC allows 10
EXCLUSIONS = ROOT / "data" / "site" / "public_float_exclusions.csv"
MANUAL_REASON = "known misreported (manual list)"


def ciks_from_raw(path, from_year):
    ciks = set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            r = json.loads(line)
            m = r.get("model_output") or {}
            year = r.get("year")
            if m.get("meeting_type") == "annual" and isinstance(year, int) and year >= from_year and r.get("cik") is not None:
                ciks.add(int(r["cik"]))
            if n % 50000 == 0:
                print(f"  read {n:,} raw records", file=sys.stderr)
    return sorted(ciks)


class RateLimiter:
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next > now:
                time.sleep(self.next - now)
                now = self.next
            self.next = now + self.interval


class Fetcher:
    def __init__(self, cache, rate, user_agent, refresh, concept=FLOAT):
        self.concept = concept
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(rate)
        self.user_agent = user_agent
        self.refresh = refresh
        self.requests = 0
        self.lock = threading.Lock()

    def path(self, cik):
        return self.cache / f"{cik:010d}.json.gz"

    def get(self, cik):
        """Cached body (bytes), b"" for a 404 (no facts), or raise after retries."""
        p = self.path(cik)
        if p.exists() and not self.refresh:
            with gzip.open(p, "rb") as f:
                return f.read()
        body = self._download(cik)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wb") as f:
            f.write(body)
        tmp.replace(p)
        return body

    def _download(self, cik):
        req = urllib.request.Request(URL.format(cik, self.concept), headers={"User-Agent": self.user_agent,
                                                               "Accept-Encoding": "gzip"})
        delay = 2.0
        for attempt in range(6):
            self.limiter.wait()
            with self.lock:
                self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    json.loads(body)          # a truncated body raises and is retried
                    return body
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return b""
                if e.code not in (403, 429, 500, 502, 503, 504):
                    raise
                err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
                err = e
            print(f"  CIK {cik}: {err}; retry {attempt + 1} in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"CIK {cik}: gave up after 6 attempts ({err})")


def facts(body, unit="USD"):
    """(entity name, list of facts in that unit) from one companyconcept response."""
    if not body:
        return None, []
    d = json.loads(body)
    return d.get("entityName"), (d.get("units") or {}).get(unit) or []


def usable(x):
    val = x.get("val")
    return isinstance(val, (int, float)) and not isinstance(val, bool) and x.get("end") and 0 < val <= MAX_FLOAT


class Shares:
    """Shares outstanding from one company's cover pages.

    for_fact(x): the largest share count on the same filing as float fact x; if
    that filing has none, the count dated nearest to the float's as-of date
    within 400 days (later date on a tie); else None.
    """

    def __init__(self, share_facts):
        self.by_accn, dated = {}, {}
        for x in share_facts:
            val, accn, end = x.get("val"), x.get("accn"), x.get("end")
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                if accn:
                    self.by_accn[accn] = max(self.by_accn.get(accn, 0), val)
                if end:
                    dated[end] = max(dated.get(end, 0), val)
        self.dated = sorted((datetime.date.fromisoformat(e).toordinal(), v) for e, v in dated.items())

    def for_fact(self, x):
        n = self.by_accn.get(x.get("accn"))
        if n or not self.dated:
            return n
        day = datetime.date.fromisoformat(x["end"]).toordinal()
        gap, _, n = min((abs(d - day), -d, v) for d, v in self.dated)
        return n if gap <= 400 else None


def best_rows(cik, usd, shares=None, dropped=None):
    """One row per as-of date: the value filed last. Applies the sanity rules.

    shares: a Shares object. A float that is more than MAX_PRICE dollars per
    share outstanding (Shares.for_fact) is a scale error (typically a figure in
    thousands tagged as dollars) and is dropped; it is appended to `dropped`
    when that list is given.
    """
    best = {}
    for x in usd:
        if not usable(x):
            continue
        val, end = x["val"], x["end"]
        n = shares.for_fact(x) if shares else None
        if n and val / n > MAX_PRICE:
            if dropped is not None:
                dropped.append([cik, end, val, x.get("accn") or "", round(val / n), "",
                                f"more than ${MAX_PRICE:,} per share outstanding"])
            continue
        key = (x.get("filed") or "", x.get("accn") or "", val)
        if end not in best or key > best[end][0]:
            best[end] = (key, x)
    rows = []
    for end in sorted(best):
        x = best[end][1]
        val = x["val"]
        rows.append([cik, end, int(val) if float(val).is_integer() else val, x.get("accn") or "",
                     x.get("form") or "", x.get("filed") or ""])
    return out_of_line(rows, dropped)


def out_of_line(rows, dropped=None):
    """Drop floats far above the company's other years (see Rules).

    rows: one company's rows in as-of order. A value v is dropped when the
    company has at least two other values, v > MAX_JUMP * median(other values),
    and the previous kept value p (if any) has v > MAX_JUMP * p. Dropped rows are
    appended to `dropped` when that list is given.
    """
    kept = []
    for i, r in enumerate(rows):
        others = [o[2] for j, o in enumerate(rows) if j != i]
        typical = statistics.median(others) if len(others) >= 2 else None
        if typical and r[2] > MAX_JUMP * typical and (not kept or r[2] > MAX_JUMP * kept[-1][2]):
            if dropped is not None:
                dropped.append([r[0], r[1], r[2], r[3], "", round(typical) if typical >= 100 else round(typical, 4),
                                f"more than {MAX_JUMP} times the company's typical float"])
            continue
        kept.append(r)
    return kept


def load_exclusions(path):
    """{cik: set of as-of dates, or {"all"}} from the manual list (see Rules)."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(int(row["cik"]), set()).add(row["as_of"].strip())
    return out


def manual_exclusions(rows, exclusions, dropped=None, matched=None):
    """Drop rows on the manual list. rows: one company's rows ([cik, as_of, value, accn, ...]).

    exclusions: from load_exclusions(). Dropped rows are appended to `dropped`;
    (cik, as_of) of each matched entry is added to `matched` ("all" counts once per row).
    """
    kept = []
    for r in rows:
        dates = exclusions.get(int(r[0]), ())
        hit = r[1] if r[1] in dates else ("all" if "all" in dates else None)
        if hit is None:
            kept.append(r)
            continue
        if matched is not None:
            matched.add((int(r[0]), hit))
        if dropped is not None:
            dropped.append([r[0], r[1], r[2], r[3], "", "", MANUAL_REASON])
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw", help="raw_extractions.jsonl.gz (v0.6.0); companies are taken from it")
    src.add_argument("--ciks", help="text file with one CIK per line, instead of --raw")
    ap.add_argument("--from-year", type=int, default=FROM_YEAR, help="first proxy year whose companies are fetched (default %(default)s)")
    ap.add_argument("--cache", default=str(ROOT / "data" / "site" / "cache" / "public_float"),
                    help="directory for downloaded responses (default: data/site/cache/public_float)")
    ap.add_argument("--out", default=str(ROOT / "data" / "site" / "public_float.csv"))
    ap.add_argument("--names-out", default=str(ROOT / "data" / "site" / "sec_entity_names.csv"))
    ap.add_argument("--dropped-out", default=str(ROOT / "data" / "site" / "public_float_dropped.csv"),
                    help="floats dropped as scale errors, for review")
    ap.add_argument("--exclusions", default=str(EXCLUSIONS), help="manual list of known misreported floats")
    ap.add_argument("--user-agent", default=USER_AGENT, help="User-Agent sent to the SEC")
    ap.add_argument("--rate", type=float, default=MAX_RATE, help="requests per second (at most 8)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refresh", action="store_true", help="download again even if a response is cached")
    args = ap.parse_args()

    if args.raw:
        ciks = ciks_from_raw(args.raw, args.from_year)
    else:
        ciks = sorted({int(line.strip()) for line in open(args.ciks, encoding="utf-8") if line.strip()})
    print(f"{len(ciks):,} companies", file=sys.stderr)

    fetcher = Fetcher(args.cache, min(args.rate, MAX_RATE), args.user_agent, args.refresh)
    share_fetcher = Fetcher(Path(args.cache).parent / "shares_outstanding", min(args.rate, MAX_RATE), args.user_agent,
                            args.refresh, SHARES)
    share_fetcher.limiter = fetcher.limiter
    started = time.monotonic()
    done = [0]

    def one(cik):
        body = fetcher.get(cik)
        _, usd = facts(body)
        if any(usable(x) and x["val"] >= CHECK_FROM for x in usd):
            body = (body, share_fetcher.get(cik))
        else:
            body = (body, b"")
        with fetcher.lock:
            done[0] += 1
            if done[0] % 500 == 0:
                print(f"  {done[0]:,}/{len(ciks):,} companies, {fetcher.requests:,} requests, "
                      f"{time.monotonic() - started:.0f}s", file=sys.stderr)
        return cik, body

    exclusions, matched = load_exclusions(args.exclusions), set()
    rows, names, dropped, no_facts, no_usable, checked = [], [], [], 0, 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for cik, (body, share_body) in pool.map(one, ciks):
            name, usd = facts(body)
            if name:
                names.append([cik, name])
            if not body:
                no_facts += 1
                continue
            checked += bool(share_body)
            r = manual_exclusions(best_rows(cik, usd, Shares(facts(share_body, "shares")[1]), dropped),
                                  exclusions, dropped, matched)
            if not r:
                no_usable += 1
            rows.extend(r)

    for path, header, data in ((args.out, ["cik", "as_of", "value_usd", "accn", "form", "filed"], rows),
                               (args.names_out, ["cik", "entity_name"], names),
                               (args.dropped_out, ["cik", "as_of", "value_usd", "accn", "usd_per_share", "typical_usd", "reason"],
                                sorted(dropped, key=lambda r: (r[0], r[1], r[3])))):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(data)
    for cik, dates in sorted(exclusions.items()):
        for d in sorted(dates - {a for c, a in matched if c == cik}):
            print(f"  manual list: CIK {cik} {d} matches no float", file=sys.stderr)
    elapsed = time.monotonic() - started
    print(f"{len(ciks):,} companies; {no_facts:,} with no public-float facts at the SEC; {no_usable:,} with facts but none "
          f"usable; {checked:,} checked against shares outstanding, {sum(1 for r in dropped if r[4] != ''):,} facts dropped as more than ${MAX_PRICE:,} per share, "
          f"{sum(1 for r in dropped if r[4] == '' and r[6] != MANUAL_REASON):,} values as out of line with the company's other years, "
          f"{sum(1 for r in dropped if r[6] == MANUAL_REASON):,} on the manual list; "
          f"{len(rows):,} rows written to {args.out}; {fetcher.requests + share_fetcher.requests:,} requests in {elapsed:.0f}s",
          file=sys.stderr)


if __name__ == "__main__":
    main()
