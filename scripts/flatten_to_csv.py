#!/usr/bin/env python3
"""Turn the raw model extraction into plain CSV tables for Stata, SAS, R or Excel.

The download holds one nested record for each proxy statement. This script writes
flat tables instead, one row per item, that any statistics package can import:

  proxy_statements.csv          one row per proxy statement
  directors.csv                 one row per director listed in a proxy statement
  director_<list>.csv           one row per item in a director's list
                                (committee_memberships, education, experience, ...)
  <list>.csv                    one row per item in a proxy statement's list
                                (director_fee_schedule, beneficial_owners_5pct, ...)
  audit_fee_lines.csv           one row per line of the auditor fee table
  audit_other_fees.csv          one row per other auditor fee

Every table carries accession_number (the SEC's ID for the proxy statement), so
tables join on it. Director tables also carry director_index, the director's
position in that proxy statement, so they join on both.

Usage:
  python scripts/flatten_to_csv.py cdd_raw_v1.1.0.zip csv_out
  python scripts/flatten_to_csv.py raw_extractions.jsonl.gz csv_out --limit 1000

Values are exactly what the model recorded; nothing is corrected. True and False
are written as 1 and 0, and missing values are left empty. Lists of plain values
(for example a director's skills) are joined with "; " in one column. Long text
columns (the director biography and the model's supporting quotes) are left out
unless you pass --with-text. Only the Python standard library is needed. The script
reads the file twice. The full file takes about three minutes and writes about 2 GB.
"""
import argparse
import csv
import gzip
import io
import json
import sys
import zipfile
from pathlib import Path

DATA_NAME = "raw_extractions.jsonl.gz"
KEYS = ["accession_number", "cik", "company_name", "filing_date", "year"]
DIRECTOR_KEYS = ["accession_number", "director_index", "director_name"]
NESTED = {"model_output", "xbrl_pvp", "audit_data", "fee_triggers", "field_evidence", "directors"}
LONG_TEXT = {"bio_raw", "evidence_for_director", "comp_footnotes"}


def records(path, limit):
    """Yield one record at a time from the .zip download or the .jsonl.gz inside it."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive, archive.open(DATA_NAME) as member:
            yield from _lines(gzip.open(member, "rt", encoding="utf-8"), limit)
    else:
        yield from _lines(gzip.open(path, "rt", encoding="utf-8"), limit)


def _lines(handle, limit):
    with handle:
        for number, line in enumerate(handle):
            if limit and number >= limit:
                return
            if line.strip():
                yield json.loads(line)


def cell(value):
    """One CSV cell: 1/0 for True/False, empty for missing, lists joined, no line breaks."""
    if value is None:
        return ""
    if value is True:
        return 1
    if value is False:
        return 0
    if isinstance(value, list):
        return "; ".join(str(cell(item)) for item in value)
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def is_plain(value):
    """True for a single value or a list of single values (not a nested object)."""
    if isinstance(value, dict):
        return False
    if isinstance(value, list):
        return not any(isinstance(item, (dict, list)) for item in value)
    return True


def plain_fields(obj, with_text):
    """The single values in obj. Nested parts and empty lists are left for their own tables."""
    return {k: cell(v) for k, v in obj.items()
            if is_plain(v) and v != [] and k not in NESTED and (with_text or k not in LONG_TEXT)}


def child_lists(obj):
    """The lists of nested objects inside obj, as (name, items)."""
    for name, value in obj.items():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            yield name, [item for item in value if isinstance(item, dict)]


def tables(record, with_text):
    """Yield (table name, row) for everything in one record."""
    ids = {k: cell(record.get(k)) for k in KEYS}
    out = record.get("model_output") or {}
    audit = out.get("audit_data") if isinstance(out.get("audit_data"), dict) else {}

    row = dict(ids)
    row.update(plain_fields(out, with_text))
    row.update(plain_fields(audit, with_text))
    row.update(plain_fields(record.get("xbrl_pvp") or {}, with_text))
    yield "proxy_statements", row

    short = {"accession_number": ids["accession_number"]}
    for name, items in child_lists(out):
        if name == "directors":
            continue
        for item in items:
            yield name, {**short, **plain_fields(item, with_text)}
    for name, items in child_lists(audit):
        for item in items:
            yield "audit_" + name, {**short, **plain_fields(item, with_text)}

    for index, director in enumerate(out.get("directors") or [], start=1):
        if not isinstance(director, dict):
            continue
        who = {**short, "director_index": index,
               "director_name": cell(director.get("director_name"))}
        yield "directors", {**ids, "director_index": index, **plain_fields(director, with_text)}
        for name, items in child_lists(director):
            for item in items:
                yield "director_" + name, {**who, **plain_fields(item, with_text)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("raw", help="cdd_raw_v1.1.0.zip, or the raw_extractions.jsonl.gz inside it")
    parser.add_argument("out", help="folder to write the CSV files into")
    parser.add_argument("--limit", type=int, default=0, help="read only the first N records (for a quick try)")
    parser.add_argument("--with-text", action="store_true", help="keep the long text columns")
    args = parser.parse_args()

    # First read: find every column each table needs, in first-seen order.
    columns = {}
    for record in records(args.raw, args.limit):
        for name, row in tables(record, args.with_text):
            seen = columns.setdefault(name, {})
            for key in row:
                seen.setdefault(key, None)

    # A list that is empty in a record looks like a plain value there. Where the list has its
    # own table, drop the stray column from the parent table.
    for name, seen in columns.items():
        prefixes = {"directors": ["director_"], "proxy_statements": ["", "audit_"]}.get(name, [])
        for key in [k for k in seen if any(p + k in columns for p in prefixes)]:
            del seen[key]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files, writers, counts = {}, {}, {}
    for name, seen in columns.items():
        files[name] = open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8")
        writers[name] = csv.DictWriter(files[name], fieldnames=list(seen), restval="", extrasaction="ignore")
        writers[name].writeheader()
        counts[name] = 0

    # Second read: write the rows.
    for record in records(args.raw, args.limit):
        for name, row in tables(record, args.with_text):
            writers[name].writerow(row)
            counts[name] += 1
    for handle in files.values():
        handle.close()

    for name in sorted(counts):
        print(f"{name}.csv: {counts[name]:,} rows, {len(columns[name])} columns")


if __name__ == "__main__":
    sys.exit(main())
