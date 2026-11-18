"""Tests for scripts/flatten_to_csv.py on a tiny made-up file.

Run: python -m unittest discover -s tests
"""

import csv
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "flatten_to_csv.py"

RECORDS = [
    {"accession_number": "a-1", "cik": "1", "company_name": "One Co", "filing_date": "2020-01-02", "year": 2020,
     "xbrl_pvp": {"pvp_tsr": None},
     "model_output": {
         "majority_voting_standard": True, "director_cohorts": ["Class I", "Class II"],
         "beneficial_owners_5pct": [{"name": "Holder", "shares": 10.0, "percent": 5.5}],
         "audit_data": {"auditor_name": "Auditor LLP", "other_fees": [],
                        "fee_lines": [{"label": "Audit fees", "amount_current": 100, "currency": "USD"}]},
         "directors": [
             {"director_name": "Ann  Lee", "age": 60, "is_independent": False, "skills": ["finance", "law"],
              "education": [{"degree": "MBA", "institution": "State"}], "experience": [],
              "bio_raw": "Line one.\nLine two."},
             {"director_name": "Bo Kim", "age": None, "is_independent": True, "skills": [],
              "education": [], "experience": [{"role": "CEO", "organization": "One Co"}]},
         ]}},
    {"accession_number": "a-2", "cik": "2", "company_name": "Two Co", "filing_date": "2021-03-04", "year": 2021,
     "xbrl_pvp": {}, "model_output": {"audit_data": None, "directors": [], "new_field": "x"}},
]


def read(folder, name):
    with open(Path(folder) / f"{name}.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class FlattenTest(unittest.TestCase):
    def run_script(self, as_zip, *extra):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        gz = tmp / "raw_extractions.jsonl.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as handle:
            for record in RECORDS:
                handle.write(json.dumps(record) + "\n")
        source = gz
        if as_zip:
            source = tmp / "cdd_raw.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.write(gz, "raw_extractions.jsonl.gz")
        subprocess.run([sys.executable, str(SCRIPT), str(source), str(tmp / "out"), *extra],
                       check=True, capture_output=True)
        return tmp / "out"

    def test_tables_from_the_zip(self):
        out = self.run_script(True)
        filings = read(out, "proxy_statements")
        self.assertEqual([r["accession_number"] for r in filings], ["a-1", "a-2"])
        self.assertEqual(filings[0]["majority_voting_standard"], "1")
        self.assertEqual(filings[0]["director_cohorts"], "Class I; Class II")
        self.assertEqual(filings[0]["auditor_name"], "Auditor LLP")
        self.assertEqual(filings[1]["new_field"], "x")
        self.assertEqual(filings[0]["new_field"], "")
        for stray in ("directors", "fee_lines", "other_fees", "beneficial_owners_5pct"):
            self.assertNotIn(stray, filings[0])

        directors = read(out, "directors")
        self.assertEqual(len(directors), 2)
        self.assertEqual(directors[0]["director_name"], "Ann Lee")
        self.assertEqual(directors[0]["is_independent"], "0")
        self.assertEqual(directors[1]["is_independent"], "1")
        self.assertEqual(directors[1]["age"], "")
        self.assertEqual(directors[0]["skills"], "finance; law")
        self.assertEqual(directors[1]["director_index"], "2")
        for stray in ("education", "experience", "bio_raw"):
            self.assertNotIn(stray, directors[0])

        self.assertEqual(read(out, "director_education")[0]["institution"], "State")
        self.assertEqual(read(out, "director_experience")[0]["director_index"], "2")
        self.assertEqual(read(out, "audit_fee_lines")[0]["amount_current"], "100")
        self.assertEqual(read(out, "beneficial_owners_5pct")[0]["percent"], "5.5")

    def test_gz_input_limit_and_text(self):
        out = self.run_script(False, "--limit", "1", "--with-text")
        self.assertEqual(len(read(out, "proxy_statements")), 1)
        self.assertEqual(read(out, "directors")[0]["bio_raw"], "Line one. Line two.")


if __name__ == "__main__":
    unittest.main()
