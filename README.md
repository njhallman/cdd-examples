# Reproduce the charts on corporatedisclosuredata.org

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/njhallman/cdd-examples/blob/main/reproduce_figures.ipynb)

This repository rebuilds every example chart on the home page of
[corporatedisclosuredata.org](https://corporatedisclosuredata.org) from the public data: the network of
companies that share directors, the company comparison, and the yearly charts on women on boards, audit
fees, governance policies, classified boards, meeting fees and director skills.

The notebook `reproduce_figures.ipynb` does it in three steps:

1. **Get the data.** It asks the same questions as the site's download form (name, email, institution,
   role and intended use), sends them to corporatedisclosuredata.org, and receives a download link that
   works for 24 hours. It then downloads the raw model extraction file (`cdd_raw_v1.1.0.zip`, about
   740 MB) and checks it. Nothing is emailed. If you already have the file, point the notebook at it and no request is sent.
2. **Build the chart data.** It runs `scripts/site/build_example_data.py`, the same script that builds the
   charts on the site, and checks that the result matches the data behind the site's charts.
3. **Draw the charts** with matplotlib and save each one to `figures/`.

The cleaned tables behind the site's Ask page are a separate dataset (version 0.7.2, beta)
and are not used here.

In Colab the first run takes about 5 to 10 minutes, mostly the download and one pass over the raw model
extraction file. Later runs reuse the download and a slim extract kept in `cdd_cache/` and take under a minute.

## Run it in Colab

Click the badge above, then *Runtime > Run all*. Fill in the form under *Get the data* (or answer the
questions as they come up).

## Run it on your own computer

```
git clone https://github.com/njhallman/cdd-examples
cd cdd-examples
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt jupyter
.venv/bin/jupyter notebook reproduce_figures.ipynb
```

Python 3.9 or later. If you already downloaded the raw model extraction file from the site, set `RAW_PATH` in the notebook
(or the environment variable `CDD_RAW_PATH`) to `raw_extractions.jsonl.gz` or to the zip. If you have a
download link from the site's email, set `CDD_DOWNLOAD_URL` instead.

## Use the data in Stata, SAS, R or Excel

The download holds one nested record for each proxy statement, which most statistics packages cannot
import directly. `scripts/flatten_to_csv.py` turns it into plain CSV tables, one row per item:

```
python3 scripts/flatten_to_csv.py cdd_raw_v1.1.0.zip csv_out
```

It needs only Python 3.9 or later, with nothing to install. The full file takes about three minutes and
writes about 2 GB of CSV files. Add `--limit 1000` for a quick try on the first 1,000 records. The main tables are:

| File | One row for each |
|---|---|
| `proxy_statements.csv` | proxy statement: governance policies, auditor, meeting details |
| `directors.csv` | director listed in a proxy statement: age, independence, pay, share ownership |
| `director_committee_memberships.csv`, `director_education.csv`, `director_experience.csv` and others | item in a director's list |
| `audit_fee_lines.csv` | line of the auditor fee table |

Every table has `accession_number`, the SEC's ID for the proxy statement, so tables join on it. Director
tables also have `director_index`. True and False are written as 1 and 0, and missing values are empty.
The values are exactly what the model recorded; nothing is corrected.

Then, for example, in Stata: `import delimited using csv_out/directors.csv, bindquote(strict) clear`.
In SAS: `proc import datafile="csv_out/directors.csv" out=directors dbms=csv replace; guessingrows=max; run;`

## What is downloaded, and from where

| What | From | When |
|---|---|---|
| Your answers to the download questions | sent to corporatedisclosuredata.org | only when the notebook requests a new link |
| `cdd_raw_v1.1.0.zip` (the raw model extraction) | the 24-hour link the site returns | once; kept in `cdd_cache/` |
| `example-data.js` (the site's chart data) | corporatedisclosuredata.org | to compare results; skipped when offline |
| Public float from SEC EDGAR | data.sec.gov | only if you turn on the optional refetch cell (about 35 minutes) |

Three things the charts need are not printed in a proxy statement. They come from public SEC data and are
stored here:

| File | Contents |
|---|---|
| `data/sic/firm_sic.parquet` | the industry (SIC) code SEC EDGAR lists for each company |
| `site/data/filings_2025.parquet` | stock ticker and exchange for the 2025 proxy statements, from the SEC's ticker file |
| `data/site/public_float.csv` | every public float each company reported to the SEC, after the checks in `scripts/site/fetch_public_float.py` |
| `data/site/public_float_exclusions.csv` | a hand-checked list of reported floats that are about 1,000 times too large |
| `data/site/public_float_dropped.csv` | every float value dropped by those checks, with the reason |
| `data/site/sec_entity_names.csv`, `site/company_display.js` | the SEC's company names and the site's brand spellings, used only to write company names in normal capitals |

The scripts in `scripts/site/` are exact copies of the ones that build the site, kept in step with them;
the definition of every number is at the top of `scripts/site/build_example_data.py`.
`scripts/cdd_download.py` handles the download request; `tests/` checks it against a local mock of the
site (`python -m unittest discover -s tests`).

## License

The code in this repository is under the MIT license (see `LICENSE`). The data are licensed under the
[Creative Commons Attribution-NonCommercial 4.0 International license (CC BY-NC 4.0)](https://corporatedisclosuredata.org/data/LICENSE).
Academic research, teaching and other non-commercial use are free, with credit to the dataset. Journalism
is also free, including at commercial news organizations; we grant that permission in addition to the
license. Any other commercial use needs our written permission first; contact the team listed on the Team
page of https://corporatedisclosuredata.org. See
[corporatedisclosuredata.org](https://corporatedisclosuredata.org) for how to cite the data. The SEC files in
`data/` and `site/` are derived from public SEC filings.

Questions about the data: data@corporatedisclosuredata.org.
