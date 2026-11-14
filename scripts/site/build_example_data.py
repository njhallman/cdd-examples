#!/usr/bin/env python3
"""Build the homepage example-chart data from the public raw extraction file.

Writes
  site/example-data.js                 the three globals the homepage charts read: D, C, SK
  data/site/example-data-report.json   every number next to the value on the original page

Run (a minute or two; the raw file is streamed once and never held in memory):

  python3 -m venv .venv && .venv/bin/pip install duckdb      # one time
  .venv/bin/python3 scripts/site/build_example_data.py --raw ~/Downloads/cdd_raw_v0.6.0/raw_extractions.jsonl.gz
  .venv/bin/python3 scripts/site/build_example_data.py --raw ... --check   # rebuild and compare with the committed files

Requirements: Python 3.9+ and the `duckdb` package (only to read two Parquet
side inputs). No network access: public float and SEC company names are
downloaded beforehand by scripts/site/fetch_public_float.py. Output is
deterministic: the same inputs give byte-identical files. Every input path can
be given as an argument (see --help); the defaults are the repository files.

`--cache PATH` keeps a slim per-filing extract (gzip JSON lines) so that a second
run skips the raw pass. The cache holds nothing that is not in the raw file.

Inputs
  --raw                                   raw_extractions.jsonl.gz, v0.6.0 (public download)
  data/sic/firm_sic.parquet               current SEC EDGAR industry (SIC) code per company
  data/site/public_float.csv              every public float reported to the SEC (fetch_public_float.py), after
                                          its automatic checks and the manual list in public_float_exclusions.csv
  data/site/sec_entity_names.csv          the SEC's name for each company (same fetch), for display casing
  site/company_display.js                 brand spellings used by the Ask page, for display casing
  site/data/filings_2025.parquet          ticker and exchange (from the SEC ticker file)
  data/site/example-data-baseline.json    the numbers embedded in the original page, for the report only

DEFINITIONS (the report file repeats these next to the numbers)

Yearly series. Every record with meeting_type == "annual" and at least one valid
director row (is_valid_director_row is not false). No de-duplication: a company
with two annual-meeting proxies in a year counts twice. Year is the raw `year`
(the filing year). Departing directors are counted as seats.
  female          mean over filings of (valid rows with gender "Female") / (valid rows)
  policies        mean of clawback_policy_exists, hedging_policy_exists, proxy_access_exists;
                  overboarding = share with overboarding_limit not null
  meeting_fee     among filings with a non-empty director_fee_schedule, share with an
                  element equal to "meeting_attendance"
  audit           see audit_fees() below
  classified      share with board_classification_type == "classified", split by public
                  float; see PublicFloat and classified_series() below
  skills          see SKILL_PATTERNS below

2025 companies (C.firms). One row per CIK: the latest 2025 annual-meeting filing
(by filing date, then accession number) with at least one valid director row.
Names are cased for display by scripts/site/company_names.py.

Network (C.net). Same 2025 filings; valid rows of directors who are not departing.
Two rows at different companies are the same person when the normalised first
and last names are identical, the middle names do not conflict (see
middle_names_agree), and both ages are present and differ by at most one year.
Because "do not conflict" is not transitive, rows are matched in pairs and never
chained into people. A line joins two companies with at least one matching pair. The chart shows
the 2-core of the largest connected group: companies are dropped, repeatedly,
while they have fewer than two links, so every dot keeps at least two lines.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime
import gzip
import json
import math
import random
import re
import statistics
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from company_names import Namer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT_JS = ROOT / "site" / "example-data.js"
OUT_REPORT = ROOT / "data" / "site" / "example-data-report.json"
BASELINE = ROOT / "data" / "site" / "example-data-baseline.json"
FIRM_SIC = ROOT / "data" / "sic" / "firm_sic.parquet"
PUBLIC_FLOAT = ROOT / "data" / "site" / "public_float.csv"
ENTITY_NAMES = ROOT / "data" / "site" / "sec_entity_names.csv"
DISPLAY_JS = ROOT / "site" / "company_display.js"
FILINGS_2025 = ROOT / "site" / "data" / "filings_2025.parquet"

FIRM_YEAR = 2025          # year of the company table and the network
PARTIAL_YEAR = 2026       # last (incomplete) year in the raw file
CLASSIFIED_FROM = 2012    # first proxy year with sound public-float coverage; see classified_series()
FLOAT_MAX_AGE = 400       # days: a public float older than this at the proxy filing date is not used
SKILL_TREND_FROM = 2010
DUMBBELL_YEARS = (2019, 2025)
LAYOUT_SEED = 20251113
LAYOUT_ITERATIONS = 300
LAYOUT_GRAVITY = 2.0      # pull towards the centre, so loose chains do not stretch the frame

# --------------------------------------------------------------------------
# Keyword lists. All are regular expressions matched case-insensitively against
# one string per director: the entries of `skills_matrix` and `skills` joined
# with " | ". The lists behind the original page were lost. These were written
# fresh as the plainest word stems for each skill name; see the report file for
# how closely each one lands on the original page's numbers.
# --------------------------------------------------------------------------
CYBER = r"cyber|information security|data security"
AI = r"artificial intelligence|\bai\b|machine learning"
CLIMATE = r"climate|sustainab|\besg\b|environmental"
DIVERSITY = r"diversity|inclusion|\bdei\b"

TREND_PATTERNS = {"cyber": CYBER, "ai": AI, "esg": CLIMATE, "dei": DIVERSITY}

# The 18 skill names are the original page's. Order here is irrelevant: the
# output is sorted by the 2025 share.
SKILL_PATTERNS = [
    ("Executive leadership", r"leadership|executive|\bceo\b"),
    ("Finance", r"financ"),
    ("Strategy", r"strateg"),
    ("Operations", r"operat"),
    ("Technology", r"technolog|digital"),
    ("Risk management", r"risk"),
    ("Corporate governance", r"governance"),
    ("International", r"international|global"),
    ("Accounting and audit", r"accounting|audit"),
    ("Mergers and acquisitions", r"merger|acquisition|m&a"),
    ("Human capital", r"human capital|human resources|talent"),
    ("Government and regulation", r"government|regulat|public policy"),
    ("Marketing and sales", r"marketing|sales"),
    ("Cybersecurity", CYBER),
    ("Climate and sustainability", CLIMATE),
    ("Legal", r"\blegal\b|\blaw\b"),
    ("Diversity and inclusion", DIVERSITY),
    ("Artificial intelligence", AI),
]

# --------------------------------------------------------------------------
# Industry: eight broad sectors from the SEC's 4-digit SIC code. First matching
# range wins. The original mapping was lost. These ranges follow the SIC
# divisions, with drugs, medical devices and health services in Healthcare and
# computers, electronics, instruments, telecoms and software in Technology. They
# were checked against the original page's label for each company: 99% agree.
# A company with no SIC code on EDGAR has no sector (null).
# --------------------------------------------------------------------------
SECTORS = ["Finance and real estate", "Healthcare", "Technology", "Manufacturing", "Consumer and retail",
           "Business services", "Energy and utilities", "Transport and construction"]
SIC_SECTOR_RANGES = [
    (100, 999, "Consumer and retail"),          # agriculture, forestry, fishing
    (1000, 1499, "Energy and utilities"),       # mining, oil and gas extraction
    (1500, 1799, "Transport and construction"),  # construction
    (2000, 2199, "Consumer and retail"),        # food, drink, tobacco
    (2830, 2836, "Healthcare"),                 # drugs and biological products
    (2900, 2999, "Energy and utilities"),       # petroleum refining
    (2200, 3569, "Manufacturing"),
    (3570, 3579, "Technology"),                 # computers and office equipment
    (3580, 3599, "Manufacturing"),
    (3600, 3699, "Technology"),                 # electronics, semiconductors
    (3700, 3819, "Manufacturing"),              # vehicles, aircraft, navigation equipment
    (3820, 3829, "Technology"),                 # measuring and laboratory instruments
    (3840, 3859, "Healthcare"),                 # medical devices
    (3860, 3999, "Manufacturing"),
    (4000, 4799, "Transport and construction"),  # rail, trucking, shipping, air, pipelines
    (4800, 4899, "Technology"),                 # telecoms and broadcasting
    (4900, 4999, "Energy and utilities"),       # electric, gas, water, waste
    (5000, 5999, "Consumer and retail"),        # wholesale and retail
    (6000, 6799, "Finance and real estate"),
    (7000, 7099, "Consumer and retail"),        # hotels
    (7370, 7379, "Technology"),                 # software and data processing
    (8000, 8099, "Healthcare"),                 # health services
    (7100, 8999, "Business services"),
    (9000, 9999, "Business services"),          # government and non-classifiable
]

# Auditor groups: first matching fragment of the lower-cased auditor name wins.
# The four Israeli firms are the local members of the Big Four networks.
AUDITOR_GROUPS = [
    ("PwC", ("pricewaterhouse", "pwc", "kesselman")),
    ("EY", ("ernst", "kost forer")),
    ("Deloitte", ("deloitte", "brightman almagor")),
    ("KPMG", ("kpmg", "somekh chaikin")),
    ("Grant Thornton", ("grant thornton",)),
    ("BDO", ("bdo",)),
    ("RSM", ("rsm", "mcgladrey")),
]
FLOAT_GROUPS = [(300e6, "Under $300M"), (2e9, "$300M to $2B"), (10e9, "$2B to $10B"), (math.inf, "$10B or more")]
FEE_QUARTILES = ["Smallest", "Lower middle", "Upper middle", "Largest"]
FEE_CATEGORIES = ("audit", "audit_related", "tax", "all_other")
FEE_UNITS = {"in thousands": 1e3, "in millions": 1e6}   # "in dollars" and "none" are taken as dollars
FEE_CEILING = 200e6       # above any fee a US company has paid its auditor; beyond it the units label is wrong


# ============================== raw pass ===================================

def reduce_record(r):
    """Keep only what the charts need from one raw record."""
    m = r.get("model_output") or {}
    a = m.get("audit_data") or {}
    directors = []
    for d in m.get("directors") or []:
        if not isinstance(d, dict):
            continue
        skills = [s for key in ("skills_matrix", "skills") for s in (d.get(key) or []) if isinstance(s, str) and s.strip()]
        age = d.get("age")
        directors.append([
            d.get("director_name") or "",
            0 if d.get("is_valid_director_row") is False else 1,
            d.get("gender"),
            age if isinstance(age, (int, float)) and not isinstance(age, bool) else None,
            1 if d.get("departing") else 0,
            " | ".join(skills),
        ])
    lines = []
    for x in a.get("fee_lines") or []:
        if isinstance(x, dict):
            amt = x.get("amount_current")
            lines.append([x.get("relation"), x.get("sec_category"),
                          amt if isinstance(amt, (int, float)) and not isinstance(amt, bool) else None,
                          x.get("units_label"), x.get("currency")])
    schedule = [x.get("element") for x in m.get("director_fee_schedule") or [] if isinstance(x, dict)]
    return {
        "acc": r.get("accession_number"), "cik": str(r.get("cik")), "name": r.get("company_name") or "",
        "fd": r.get("filing_date") or "", "year": r.get("year"), "mt": m.get("meeting_type"),
        "claw": bool(m.get("clawback_policy_exists")), "hedge": bool(m.get("hedging_policy_exists")),
        "proxy": bool(m.get("proxy_access_exists")), "overboard": m.get("overboarding_limit") is not None,
        "cls": m.get("board_classification_type"), "dual": bool(m.get("dual_class_structure")),
        "sched": 0 if not schedule else (2 if "meeting_attendance" in schedule else 1),
        "auditor": a.get("auditor_name") or "", "fee_flag": bool(a.get("fee_flag")), "lines": lines,
        "d": directors,
    }


def records(raw, cache):
    """Yield reduced records from the cache if it exists, else from the raw file (writing the cache)."""
    if cache and Path(cache).exists():
        with gzip.open(cache, "rt", encoding="utf-8") as f:
            for line in f:
                yield json.loads(line)
        return
    if not raw:
        sys.exit("--raw PATH is required (the public raw_extractions.jsonl.gz)")
    out = gzip.open(str(cache) + ".tmp", "wt", encoding="utf-8", compresslevel=3) if cache else None
    with gzip.open(raw, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            rec = reduce_record(json.loads(line))
            if out:
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
            if n % 20000 == 0:
                print(f"  read {n:,} raw records", file=sys.stderr)
            yield rec
    if out:
        out.close()
        Path(str(cache) + ".tmp").replace(cache)


# ============================== audit fees =================================

def audit_fees(rec):
    """(total fees in dollars, category shares or None) for one filing, or None.

    Each fee line carries a `relation`:
      line         a fee the company paid
      total        the total the proxy prints under the table
      included_in  an "of which" amount already inside another line: ignored
      subtotal, excluded: ignored
    Total. The printed total when there is one, otherwise the sum of the lines.
    The printed total is preferred because it is the company's own figure and it
    is what the processed release (and Ask the data) reports.
    Units. Amounts are multiplied by the line's own units label (thousands,
    millions) and are otherwise as printed; the raw file does not correct
    mislabelled units. One guard: if the scaled total is above FEE_CEILING, the
    label is taken to be wrong and every amount is read as dollars.
    Mix. Each category's share is its lines divided by the sum of the lines of
    all four categories, so the four shares add to one. A filing with no
    categorised line (a printed total only) has a total but no mix.
    Left out. Filings the release flags (`fee_flag`), filings with a counted
    amount not in US dollars or below zero, and totals that are not positive or
    are still above the ceiling.
    """
    if rec["fee_flag"]:
        return None
    for scaled in (True, False):
        cats = dict.fromkeys(FEE_CATEGORIES, 0.0)
        line_sum, have_line, have_cat, printed_total = 0.0, False, False, None
        for relation, category, amount, units, currency in rec["lines"]:
            if amount is None or relation not in ("line", "total"):
                continue
            if currency not in (None, "", "USD") or amount < 0:
                return None
            value = amount * (FEE_UNITS.get(units, 1) if scaled else 1)
            if relation == "total":
                if printed_total is None:
                    printed_total = value
                continue
            line_sum += value
            have_line = True
            if category in cats:
                cats[category] += value
                have_cat = True
        total = printed_total if printed_total is not None else line_sum if have_line else None
        if total is None or total <= 0:
            return None
        if total <= FEE_CEILING:
            cat_sum = sum(cats.values())
            shares = {c: v / cat_sum for c, v in cats.items()} if have_cat and cat_sum > 0 else None
            return total, shares
    return None


# ============================== people =====================================

HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor", "hon", "honorable", "sir", "dame", "lord", "gen",
              "general", "adm", "admiral", "lt", "col", "colonel", "maj", "capt", "captain", "rev", "reverend", "amb",
              "ambassador", "sen", "senator", "gov", "judge", "ret", "retired", "vice", "rear", "brig", "cmdr"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
CREDENTIALS = {"phd", "md", "mba", "jd", "esq", "cpa", "cfa", "mph", "ms", "msc", "ma", "dphil", "pharmd", "rph", "scd",
               "dsc", "dds", "dvm", "mbbs", "edd", "llm", "facp", "facs", "do", "rn", "pe", "cbe", "obe", "frs"}


def normalise_name(name):
    """Normalised director name as a list of words, or None if fewer than two words remain.

    Accents and case are removed; bracketed and quoted nicknames are dropped;
    everything after the first comma is dropped (", Jr.", ", Ph.D."); hyphens and
    apostrophes are closed up; other punctuation becomes a space; leading
    honorifics and trailing Jr/Sr/II/III/IV/V or degree letters are dropped.
    Middle names and initials are kept as written; the first word is the first
    name, the last word the last name, and the words between are middle names.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r'["“”‘][^"“”’]*["”’]', " ", s)
    s = s.split(",")[0]
    s = s.lower().replace("-", "").replace("'", "").replace("’", "")
    s = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\b)", ". ", s)
    toks = re.sub(r"[^a-z ]", " ", s.replace(".", " ")).split()
    while toks and toks[0] in HONORIFICS:
        toks = toks[1:]
    while len(toks) > 2 and (toks[-1] in SUFFIXES or toks[-1] in CREDENTIALS):
        toks = toks[:-1]
    while len(toks) > 3 and "".join(toks[-2:]) in CREDENTIALS:
        toks = toks[:-2]
    return toks if len(toks) >= 2 else None


def middle_names_agree(a, b):
    """True unless two middle-name lists conflict.

    A side with no middle name matches anything. Otherwise the lists are compared
    word by word from the left: equal words agree, an initial agrees with any word
    starting with that letter ("A" with "Alan"), and anything else conflicts. Words
    beyond the end of the shorter list are ignored ("A" agrees with "A B").
    """
    if a == b or not a or not b:
        return True
    for x, y in zip(a, b):
        if x == y:
            continue
        if (len(x) == 1 or len(y) == 1) and x[0] == y[0]:
            continue
        return False
    return True


def build_network(firms):
    """firms: list of dicts with cik and rec. Returns (person_pairs, edges as sorted (cik, cik) list).

    Rows are grouped by first and last name; within a group every pair of rows at
    different companies is tested directly (middle names agree, ages within one
    year). Pairs are not chained, so A~B and B~C does not make A~C.
    """
    by_name = collections.defaultdict(list)
    for f in firms:
        for name, valid, _gender, age, departing, _skills in f["rec"]["d"]:
            if not valid or departing or age is None:
                continue
            toks = normalise_name(name)
            if toks:
                by_name[(toks[0], toks[-1])].append((f["cik"], age, toks[1:-1]))
    pairs = 0
    edges = set()
    for rows in by_name.values():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                (ca, aa, ma), (cb, ab, mb) = rows[i], rows[j]
                if ca != cb and abs(aa - ab) <= 1 and middle_names_agree(ma, mb):
                    pairs += 1
                    edges.add((ca, cb) if ca < cb else (cb, ca))
    return pairs, sorted(edges)


def giant_and_two_core(edges):
    adj = collections.defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    seen, giant = set(), []
    for start in sorted(adj):
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        if len(comp) > len(giant):
            giant = comp
    core = set(giant)
    deg = {u: len(adj[u]) for u in core}
    queue = [u for u in core if deg[u] < 2]
    while queue:
        u = queue.pop()
        if u not in core:
            continue
        core.discard(u)
        for v in adj[u]:
            if v in core:
                deg[v] -= 1
                if deg[v] < 2:
                    queue.append(v)
    return len(adj), len(giant), core


def layout(n, edges, seed=LAYOUT_SEED, iterations=LAYOUT_ITERATIONS, gravity=LAYOUT_GRAVITY):
    """Seeded Fruchterman-Reingold layout in pure Python. Returns x, y lists scaled to 0-1.

    Repulsion k*k/d between every pair, attraction d*d/k along every line, a pull
    of gravity*d towards the centre, and a step limit that cools linearly to zero. Repulsion is exact for dots in the same
    or a neighbouring cell of a 16 by 16 grid and uses each other cell's centre of
    mass beyond that. Only +, -, *, / and sqrt are used, so the result is the same
    on every machine.
    """
    rnd = random.Random(seed)
    x = [rnd.random() for _ in range(n)]
    y = [rnd.random() for _ in range(n)]
    k = 1.0 / math.sqrt(n)
    kk = k * k
    grid = 16
    temp0 = 0.1
    for it in range(iterations):
        temp = temp0 * (1.0 - it / iterations)
        minx, maxx, miny, maxy = min(x), max(x), min(y), max(y)
        sx = grid / ((maxx - minx) or 1.0) * 0.999999
        sy = grid / ((maxy - miny) or 1.0) * 0.999999
        cells = [[] for _ in range(grid * grid)]
        cell_of = [0] * n
        for i in range(n):
            c = int((x[i] - minx) * sx) * grid + int((y[i] - miny) * sy)
            cell_of[i] = c
            cells[c].append(i)
        centre = []
        for c, members in enumerate(cells):
            if members:
                m = len(members)
                centre.append((c // grid, c % grid, m, sum(x[i] for i in members) / m, sum(y[i] for i in members) / m))
        dx = [0.0] * n
        dy = [0.0] * n
        for i in range(n):
            xi, yi = x[i], y[i]
            gx, gy = divmod(cell_of[i], grid)
            fx = fy = 0.0
            for cx, cy, m, mx, my in centre:
                if -1 <= cx - gx <= 1 and -1 <= cy - gy <= 1:
                    for j in cells[cx * grid + cy]:
                        if j != i:
                            ax = xi - x[j]
                            ay = yi - y[j]
                            d2 = ax * ax + ay * ay
                            if d2 < 1e-12:
                                ax = 1e-4 * (1 if i < j else -1)
                                d2 = 1e-8
                            fx += kk * ax / d2
                            fy += kk * ay / d2
                else:
                    ax = xi - mx
                    ay = yi - my
                    d2 = ax * ax + ay * ay
                    w = kk * m / d2
                    fx += w * ax
                    fy += w * ay
            dx[i] = fx
            dy[i] = fy
        for a, b in edges:
            ax = x[a] - x[b]
            ay = y[a] - y[b]
            d = math.sqrt(ax * ax + ay * ay)
            w = d / k
            dx[a] -= w * ax
            dy[a] -= w * ay
            dx[b] += w * ax
            dy[b] += w * ay
        mx = sum(x) / n
        my = sum(y) / n
        for i in range(n):
            dx[i] -= gravity * (x[i] - mx)
            dy[i] -= gravity * (y[i] - my)
            d = math.sqrt(dx[i] * dx[i] + dy[i] * dy[i])
            if d > 0:
                step = min(d, temp) / d
                x[i] += dx[i] * step
                y[i] += dy[i] * step
    minx, maxx, miny, maxy = min(x), max(x), min(y), max(y)
    return ([round((v - minx) / (maxx - minx), 4) for v in x],
            [round((v - miny) / (maxy - miny), 4) for v in y])


# ============================== side inputs ================================

def load_side_inputs(firm_sic, filings_2025):
    try:
        import duckdb
    except ImportError:
        sys.exit("This script needs the duckdb package to read the Parquet side inputs.\n"
                 "  python3 -m venv .venv && .venv/bin/pip install duckdb\n"
                 "  .venv/bin/python3 scripts/site/build_example_data.py --raw PATH")
    con = duckdb.connect()
    sic = {str(int(cik)): code for cik, code in
           con.execute(f"select cik, sic from '{firm_sic}' where sic is not null").fetchall()}
    ticker_by_acc, ticker_by_cik = {}, {}
    for acc, cik, fd, ticker, exchange in con.execute(
            f"select accession_number, cik, filing_date, ticker, exchange from '{filings_2025}' "
            "order by filing_date, accession_number").fetchall():
        ticker_by_acc[acc] = (ticker or "", exchange)
        ticker_by_cik[str(int(cik))] = (ticker or "", exchange)
    return sic, ticker_by_acc, ticker_by_cik


class PublicFloat:
    """Public float at a proxy filing date, from data/site/public_float.csv.

    The table (scripts/site/fetch_public_float.py) has one row per company and
    as-of date: every dei:EntityPublicFloat value the company reported to the
    SEC, in US dollars and above zero. For a proxy filed on date F the float is
    the row with the latest as-of date A such that A <= F and F - A <= 400 days
    (FLOAT_MAX_AGE); if there is none, the proxy has no float. The as-of date is
    the end of the company's second fiscal quarter, so for a calendar-year
    company the float is usually the 30 June of the year before the proxy.
    """

    def __init__(self, path):
        rows = collections.defaultdict(list)
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                value = float(row["value_usd"])
                if value > 0:
                    rows[str(int(row["cik"]))].append((datetime.date.fromisoformat(row["as_of"]).toordinal(), value))
        self.dates = {c: [d for d, _ in sorted(v)] for c, v in rows.items()}
        self.values = {c: [x for _, x in sorted(v)] for c, v in rows.items()}
        self.ciks = len(rows)

    def at(self, cik, filing_date):
        dates = self.dates.get(cik)
        if not dates or not filing_date:
            return None
        fd = datetime.date.fromisoformat(filing_date).toordinal()
        i = bisect.bisect_right(dates, fd) - 1
        if i < 0 or fd - dates[i] > FLOAT_MAX_AGE:
            return None
        return self.values[cik][i]


def sector_for(sic):
    if not sic:
        return None
    code = int(sic)
    for lo, hi, sector in SIC_SECTOR_RANGES:
        if lo <= code <= hi:
            return sector
    return None


def auditor_group(name):
    low = name.lower()
    if not low.strip():
        return None
    for group, fragments in AUDITOR_GROUPS:
        if any(f in low for f in fragments):
            return group
    return "Other firms"


def float_group(value):
    if value is None:
        return None
    for limit, label in FLOAT_GROUPS:
        if value < limit:
            return label


def percentile(sorted_values, q):
    """Linear-interpolation percentile (the usual spreadsheet definition)."""
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def tidy(v):
    """Whole numbers as ints so the JSON has no trailing .0."""
    return int(v) if isinstance(v, float) and v == int(v) else v


# ============================== build ======================================

def build(raw, cache, inputs):
    sic, ticker_by_acc, ticker_by_cik = load_side_inputs(inputs.firm_sic, inputs.filings_2025)
    floats = PublicFloat(inputs.public_float)
    display_name = Namer(inputs.display_js, inputs.entity_names)
    trend_re = {k: re.compile(v, re.I) for k, v in TREND_PATTERNS.items()}
    skill_re = [(name, re.compile(p, re.I)) for name, p in SKILL_PATTERNS]
    dd = collections.defaultdict

    n_raw = 0
    tiles = {"filings": 0, "director_rows": 0}
    ciks_all = set()
    years_seen = set()
    first_year = {}
    n_annual = collections.Counter()
    female = dd(list)
    pol = {k: dd(int) for k in ("clawback", "hedging", "proxy_access", "overboarding")}
    sched = dd(lambda: [0, 0])
    fee_totals = dd(list)
    fee_mix = dd(lambda: {c: [] for c in FEE_CATEGORIES})
    classified = dd(list)                     # year -> [(float, cik, is_classified)]
    classified_all = dd(lambda: [0, 0])
    skill_den = collections.Counter()
    skill_hit = {k: collections.Counter() for k in TREND_PATTERNS}
    dumb_den = collections.Counter()
    dumb_hit = {y: collections.Counter() for y in DUMBBELL_YEARS}
    latest = {}

    for rec in records(raw, cache):
        n_raw += 1
        year, cik = rec["year"], rec["cik"]
        rows = rec["d"]
        if not rows:
            continue
        tiles["filings"] += 1
        ciks_all.add(cik)
        years_seen.add(year)
        valid = [d for d in rows if d[1]]
        tiles["director_rows"] += len(valid)
        if cik not in first_year or year < first_year[cik]:
            first_year[cik] = year
        if rec["mt"] != "annual" or not valid:
            continue
        n_annual[year] += 1
        female[year].append(sum(1 for d in valid if d[2] == "Female") / len(valid))
        pol["clawback"][year] += rec["claw"]
        pol["hedging"][year] += rec["hedge"]
        pol["proxy_access"][year] += rec["proxy"]
        pol["overboarding"][year] += rec["overboard"]
        if rec["sched"]:
            sched[year][1] += 1
            sched[year][0] += rec["sched"] == 2
        fees = audit_fees(rec)
        if fees:
            fee_totals[year].append(fees[0])
            if fees[1]:
                for c in FEE_CATEGORIES:
                    fee_mix[year][c].append(fees[1][c])
        is_classified = rec["cls"] == "classified"
        classified_all[year][0] += is_classified
        classified_all[year][1] += 1
        fl = floats.at(cik, rec["fd"])
        if fl is not None:
            classified[year].append((fl, cik, is_classified))
        texts = [d[5] for d in valid if d[5]]
        if texts:
            skill_den[year] += 1
            joined = " | ".join(texts)
            for k, rx in trend_re.items():
                if rx.search(joined):
                    skill_hit[k][year] += 1
            if year in dumb_hit:
                dumb_den[year] += len(texts)
                for text in texts:
                    for name, rx in skill_re:
                        if rx.search(text):
                            dumb_hit[year][name] += 1
        if year == FIRM_YEAR:
            prev = latest.get(cik)
            if prev is None or (rec["fd"], rec["acc"]) > (prev["fd"], prev["acc"]):
                latest[cik] = rec

    years = sorted(n_annual)

    def series(fn, yrs=years, nd=4):
        return {str(y): round(fn(y), nd) for y in yrs}

    # ---- D
    D = {}
    D["tiles"] = {"filings": tiles["filings"], "companies": len(ciks_all), "director_rows": tiles["director_rows"],
                  "first_year": min(years_seen), "last_year": max(years_seen)}
    D["female"] = series(lambda y: statistics.fmean(female[y]))
    D["policies"] = {k: series(lambda y, k=k: pol[k][y] / n_annual[y]) for k in pol}
    D["meeting_fee"] = series(lambda y: sched[y][0] / sched[y][1])
    fee_years = [y for y in years if y >= 2001]
    D["audit_median_total"] = {str(y): int(round(statistics.median(fee_totals[y]))) for y in fee_years}
    D["audit_n"] = {str(y): len(fee_totals[y]) for y in fee_years}
    D["audit_mix"] = {c: series(lambda y, c=c: statistics.fmean(fee_mix[y][c]), fee_years) for c in FEE_CATEGORIES}
    D["skills"] = {k: series(lambda y, k=k: skill_hit[k][y] / skill_den[y]) for k in ("cyber", "ai")}
    D["classified"] = classified_series(classified, n_annual)
    extra = {
        "raw_records": n_raw,
        "annual_filings": {str(y): n_annual[y] for y in years},
        "skills_filings": {str(y): skill_den[y] for y in years},
        "audit_mix_n": {str(y): len(fee_mix[y]["audit"]) for y in fee_years},
        "classified_all_companies": {str(y): round(classified_all[y][0] / classified_all[y][1], 4) for y in years},
        "classified_float_coverage": {str(y): round(len(classified[y]) / n_annual[y], 4) for y in years if y >= 2009},
        "classified_top_quarter_cut_usd": {str(y): int(round(percentile(sorted(r[0] for r in classified[y]), 0.75)))
                                           for y in years if y >= CLASSIFIED_FROM and classified[y]},
        "public_float_companies_in_table": floats.ciks,
        "dumbbell_directors": {str(y): dumb_den[y] for y in DUMBBELL_YEARS},
    }

    # ---- SK
    trend_years = [y for y in years if y >= SKILL_TREND_FROM]
    SK = {"trend": {k: series(lambda y, k=k: skill_hit[k][y] / skill_den[y], trend_years) for k in TREND_PATTERNS}}
    y0, y1 = DUMBBELL_YEARS
    dumb = [{"name": name, f"y{y0}": round(dumb_hit[y0][name] / dumb_den[y0], 4),
             f"y{y1}": round(dumb_hit[y1][name] / dumb_den[y1], 4)} for name, _ in SKILL_PATTERNS]
    SK["dumbbell"] = sorted(dumb, key=lambda r: (-r[f"y{y1}"], r["name"]))

    # ---- C.firms
    firm_ciks = sorted(latest, key=int)
    firms = []
    for cik in firm_ciks:
        rec = latest[cik]
        name = display_name(rec["name"], cik)
        valid = [d for d in rec["d"] if d[1]]
        ages = [d[3] for d in valid if d[3] is not None]
        n_female = sum(1 for d in valid if d[2] == "Female")
        fees = audit_fees(rec)
        ticker, exchange = ticker_by_acc.get(rec["acc"]) or ticker_by_cik.get(cik) or ("", None)
        fy = first_year[cik]
        firms.append({"cik": cik, "rec": rec, "row": [
            name, ticker, sector_for(sic.get(cik)), round(n_female / len(valid), 3),
            tidy(round(fees[0])) if fees else None,
            tidy(statistics.median(ages)) if ages else None,
            len(valid), None, n_female,
            exchange or "No exchange listed", auditor_group(rec["auditor"]), None,
            "2000 or earlier" if fy <= 2000 else "2001 to 2010" if fy <= 2010 else "2011 to 2020" if fy <= 2020 else "2021 or later",
            "Dual-class shares" if rec["dual"] else "Single class of shares",
            float_group(floats.at(cik, rec["fd"])),
        ]})
    ranked = sorted((f for f in firms if f["row"][4] is not None), key=lambda f: (f["row"][4], int(f["cik"])))
    for i, f in enumerate(ranked):
        f["row"][11] = FEE_QUARTILES[min(3, i * 4 // len(ranked))]

    # ---- C.net
    pairs, edges = build_network(firms)
    linked, giant, core = giant_and_two_core(edges)
    core_ciks = sorted(core, key=int)
    index = {c: i for i, c in enumerate(core_ciks)}
    net_edges = sorted((min(index[a], index[b]), max(index[a], index[b])) for a, b in edges if a in index and b in index)
    degree = collections.Counter()
    for a, b in net_edges:
        degree[a] += 1
        degree[b] += 1
    xs, ys = layout(len(core_ciks), net_edges)
    by_cik = {f["cik"]: f["row"] for f in firms}
    nodes = [[xs[i], ys[i], by_cik[c][0], by_cik[c][2], degree[i]] for i, c in enumerate(core_ciks)]
    C = {"firms": [f["row"] for f in firms],
         "net": {"n_firms": len(firms), "giant": giant, "nodes": nodes, "edges": [list(e) for e in net_edges]}}
    extra["network"] = {"person_pairs": pairs, "company_links": len(edges), "companies_with_a_link": linked,
                        "largest_group": giant, "companies_plotted": len(nodes), "links_plotted": len(net_edges),
                        "links_within_one_industry": sum(1 for a, b in net_edges if nodes[a][3] == nodes[b][3]),
                        "max_links": max(degree.values()), "min_links": min(degree.values())}
    return D, C, SK, extra


def classified_series(classified, n_annual):
    """Classified boards by public-float quarter.

    A filing enters when it has a public float (PublicFloat.at: the latest float
    the company reported to the SEC within 400 days before the proxy filing date).
    Quarters are cut within each proxy year at the 25th, 50th and 75th
    percentiles of those filings' floats; a filing on a cut point goes to the
    higher quarter, so the top quarter is every filing at or above the 75th
    percentile. `coverage` is the share of that year's annual-meeting filings
    with a float. Years before CLASSIFIED_FROM are left out: the SEC phased in
    the float tag for smaller companies only for fiscal periods after 15 June
    2011, so in 2010 and 2011 the filings with a float are mostly the largest
    companies (see classified_float_coverage in the report).
    """
    out = {k: {} for k in ("top", "second", "third", "bottom", "top2", "rest", "n_top", "n", "coverage")}
    for y in sorted(classified):
        if y < CLASSIFIED_FROM:
            continue
        rows = classified[y]
        values = sorted(r[0] for r in rows)
        q1, q2, q3 = (percentile(values, q) for q in (0.25, 0.5, 0.75))
        groups = {"top": [], "second": [], "third": [], "bottom": []}
        for fl, _cik, is_cls in rows:
            groups["top" if fl >= q3 else "second" if fl >= q2 else "third" if fl >= q1 else "bottom"].append(is_cls)
        rest = groups["second"] + groups["third"] + groups["bottom"]
        ys = str(y)
        for g in ("top", "second", "third", "bottom"):
            out[g][ys] = round(sum(groups[g]) / len(groups[g]), 3)
        out["top2"][ys] = out["top"][ys]
        out["rest"][ys] = round(sum(rest) / len(rest), 3)
        out["n_top"][ys] = len(groups["top"])
        out["n"][ys] = len(rows)
        out["coverage"][ys] = round(len(rows) / n_annual[y], 4)
    full = [n for y, n in out["n_top"].items() if int(y) != PARTIAL_YEAR]
    out["n_top_avg"] = int(round(statistics.fmean(full)))
    out["n_top_min"] = min(full)
    out["n_top_max"] = max(full)
    return out


# ============================== report =====================================

DEFINITIONS = {
    "yearly_population": "Every annual-meeting proxy (meeting_type == 'annual') with at least one valid director row, by filing year. "
                         "A company with two such proxies in a year counts twice. Departing directors count as seats.",
    "tiles": "Proxy statements = raw records with at least one director row. Companies = distinct CIKs among them. "
             "director_rows = valid director rows in those records (one row per director per proxy, not distinct people).",
    "female": "Average across proxies of the share of valid director rows whose gender is Female.",
    "policies": "Share of proxies where the extraction found the policy: clawback_policy_exists, hedging_policy_exists, "
                "proxy_access_exists; overboarding = overboarding_limit is not null.",
    "meeting_fee": "Among proxies with a director fee schedule, the share with a meeting_attendance element.",
    "audit": "Total = the printed total if there is one, else the sum of the fee lines, scaled by the units label; if that gives "
             "more than $200 million the label is taken to be wrong and amounts are read as dollars. 'Of which' lines and subtotals "
             "are ignored. Filings with fees not in US dollars are left out. Median across proxies, nominal dollars. "
             "Mix = each of the four SEC categories as a share of the four together, averaged across proxies.",
    "classified": "Share with board_classification_type == 'classified'. Public float: the latest figure the company reported to "
                  "the SEC within 400 days before the proxy statement (as-of date on or before the filing date and at most 400 "
                  "days earlier). Top quarter = float at or above the 75th percentile of that year's proxies with a float. "
                  "Proxies with no float are in neither line. coverage = share of the year's proxies with a float.",
    "skills_trend": "Among proxies where at least one valid director row has a skills or skills_matrix entry, the share where "
                    "any director's entries match the topic's keyword list.",
    "skills_dumbbell": "Among valid director rows with a skills or skills_matrix entry, in annual-meeting proxies of that year, "
                       "the share whose entries match the skill's keyword list.",
    "firms": "One row per CIK: the latest 2025 annual-meeting proxy with a valid director row. Board size, women and median age "
             "use all valid director rows. First proxy year is the CIK's earliest record in the raw file. Share class is "
             "dual_class_structure. Auditor group is from the auditor's name; members of a Big Four network count with it. "
             "Public float group: the latest public float the company reported to the SEC within 400 days before that proxy.",
    "network": "2025 companies as above; valid rows of directors who are not departing. Two rows at different companies are the "
               "same person when the normalised first and last names are identical, middle names or initials do not conflict "
               "(missing on one side matches anything; an initial matches a name starting with it), and both ages are present "
               "and within one year. Rows are matched in pairs, not chained into people. A line joins two companies with at "
               "least one matching pair. Plotted: the largest connected group after repeatedly "
               "dropping companies with fewer than two links. Positions come from a seeded force-directed layout.",
}
OUTSIDE_INPUTS = {
    "industry": "data/sic/firm_sic.parquet: the SIC code SEC EDGAR shows for the company now, grouped into eight sectors by the ranges below.",
    "public_float": "data/site/public_float.csv (scripts/site/fetch_public_float.py): every dei:EntityPublicFloat value in US dollars "
                    "each company reported to the SEC (SEC XBRL companyconcept API), one row per company and as-of date, the "
                    "latest filed. Zero, negative and above-$10-trillion values are dropped, and so are scale errors: a value "
                    "above $10,000 per share outstanding, or more than 50 times the median of the company's other values "
                    "without a gradual rise to it, and the values on a short manual list of companies whose reported floats "
                    "are wrong in most years or too few to check (data/site/public_float_exclusions.csv); every dropped value "
                    "is listed in data/site/public_float_dropped.csv.",
    "company_names": "The name filed in the raw file, re-cased only: data/site/sec_entity_names.csv (the SEC's entityName, same "
                     "fetch) guides the casing of the same letters, and the Ask page's brand spellings (site/company_display.js); "
                     "rules in scripts/site/company_names.py.",
    "ticker_exchange": "site/data/filings_2025.parquet: ticker and exchange as published in the Ask the data subset.",
}

def compare_series(new, old):
    common = [y for y in new if y in old]
    if not common:
        return None
    worst = max(common, key=lambda y: (abs(new[y] - old[y]), y))
    fy = str(FIRM_YEAR)
    return {"years_compared": len(common), "max_abs_diff": round(abs(new[worst] - old[worst]), 4), "year_of_max": int(worst),
            "old_2025": old.get(fy), "new_2025": new.get(fy)}


def build_report(D, C, SK, extra):
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    rep = {
        "note": "Old = the numbers embedded in the original homepage (built from the processed release; no script survived). "
                "New = scripts/site/build_example_data.py on the public raw file v0.6.0.",
        "definitions": DEFINITIONS,
        "outside_inputs": OUTSIDE_INPUTS,
        "counts": extra,
        "keyword_lists": {"trend": TREND_PATTERNS, "dumbbell": dict(SKILL_PATTERNS)},
        "sic_sector_ranges": [[lo, hi, s] for lo, hi, s in SIC_SECTOR_RANGES],
    }
    col = lambda i: dict(sorted(collections.Counter(str(r[i]) for r in C["firms"]).items()))
    new_firms = {"rows": len(C["firms"]), "industry": col(2), "exchange": col(9), "auditor": col(10),
                 "first_proxy_year": col(12), "share_class": col(13), "public_float": col(14),
                 "audit_fee_missing": sum(1 for r in C["firms"] if r[4] is None),
                 "age_missing": sum(1 for r in C["firms"] if r[5] is None),
                 "ticker_missing": sum(1 for r in C["firms"] if not r[1])}
    net = C["net"]
    new_net = {"n_firms": net["n_firms"], "giant": net["giant"], "nodes": len(net["nodes"]), "edges": len(net["edges"])}
    if not base:
        rep["firms"] = {"new": new_firms}
        rep["network"] = {"new": new_net}
        return rep
    bD, bSK = base["D"], base["SK"]
    s = {}
    s["female"] = compare_series(D["female"], bD["female"])
    s["meeting_fee"] = compare_series(D["meeting_fee"], bD["meeting_fee"])
    for k in D["policies"]:
        s["policies." + k] = compare_series(D["policies"][k], bD["policies"][k])
    s["audit_median_total"] = compare_series(D["audit_median_total"], bD["audit_median_total"])
    worst = max(D["audit_median_total"], key=lambda y: abs(D["audit_median_total"][y] / bD["audit_median_total"][y] - 1))
    s["audit_median_total"]["max_pct_diff"] = round(100 * abs(D["audit_median_total"][worst] / bD["audit_median_total"][worst] - 1), 2)
    s["audit_median_total"]["year_of_max_pct"] = int(worst)
    s["audit_n"] = compare_series(D["audit_n"], bD["audit_n"])
    for k in D["audit_mix"]:
        s["audit_mix." + k] = compare_series(D["audit_mix"][k], bD["audit_mix"][k])
    for k in ("top2", "rest", "n_top", "n"):
        s["classified." + k] = compare_series(D["classified"][k], bD["classified"][k])
    for k in D["skills"]:
        s["skills." + k] = compare_series(D["skills"][k], bD["skills"][k])
    for k in SK["trend"]:
        s["trend." + k] = compare_series(SK["trend"][k], bSK["trend"][k])
    rep["series"] = s
    old_d = {r["name"]: r for r in bSK["dumbbell"]}
    rep["dumbbell"] = [{"name": r["name"], "old_2019": old_d[r["name"]]["y2019"], "new_2019": r["y2019"],
                        "old_2025": old_d[r["name"]]["y2025"], "new_2025": r["y2025"]} for r in SK["dumbbell"]]
    rep["tiles"] = {"old": bD["tiles"], "new": D["tiles"]}
    rep["firms"] = {"old": base["C"]["firms"], "new": new_firms}
    rep["network"] = {"old": base["C"]["net"], "new": new_net}
    rep["not_rebuilt"] = [
        "tiles.directors (distinct people): needs person identity across 30 years; replaced by director_rows, the count of valid director rows",
        "C.firms column 7 (median director pay): left null; the page no longer plots it",
        "C.skills (an unused ten-group skills breakdown): dropped",
        "classified boards before 2012: left out; the SEC phased in the float tag for smaller companies only in 2011, so earlier years cover mostly large companies",
        "D.skills (cyber and AI from 1997, not drawn): now the same rule as the trend chart, so it differs slightly from the old one",
    ]
    return rep


def render_js(D, C, SK):
    dump = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False)
    return ("// Generated by scripts/site/build_example_data.py from the public raw file. Do not edit by hand.\n"
            f"const D = {dump(D)};\nconst C = {dump(C)};\nconst SK = {dump(SK)};\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", help="path to raw_extractions.jsonl.gz (v0.6.0)")
    ap.add_argument("--cache", help="optional slim per-filing extract; written on the first run, read afterwards")
    ap.add_argument("--check", action="store_true", help="rebuild and compare with the committed files; write nothing")
    ap.add_argument("--public-float", default=str(PUBLIC_FLOAT), help="public_float.csv from fetch_public_float.py")
    ap.add_argument("--entity-names", default=str(ENTITY_NAMES), help="sec_entity_names.csv from fetch_public_float.py")
    ap.add_argument("--display-js", default=str(DISPLAY_JS), help="site/company_display.js (brand spellings)")
    ap.add_argument("--firm-sic", default=str(FIRM_SIC), help="firm_sic.parquet (needs duckdb)")
    ap.add_argument("--filings-2025", default=str(FILINGS_2025), help="filings_2025.parquet with ticker and exchange (needs duckdb)")
    args = ap.parse_args()
    D, C, SK, extra = build(args.raw, args.cache, args)
    js = render_js(D, C, SK)
    report = json.dumps(build_report(D, C, SK, extra), indent=1, ensure_ascii=False) + "\n"
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, text in ((OUT_JS, js), (OUT_REPORT, report))
                 if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            sys.exit("Out of date: " + ", ".join(stale))
        print("example data is up to date")
        return
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_JS.write_text(js, encoding="utf-8")
    OUT_REPORT.write_text(report, encoding="utf-8")
    print(f"wrote {OUT_JS.relative_to(ROOT)} ({len(js):,} bytes) and {OUT_REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
