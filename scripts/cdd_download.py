"""Get the raw model extraction file for the notebook (standard library only).

The raw model extraction is free, but the site asks a few questions before it
hands out a download link: the same questions as the download form on
https://corporatedisclosuredata.org/#download. `request_links` sends the
answers to the site and gets back links that work for 24 hours; nothing is
emailed. `download` saves a link to disk with a progress line, and `unpack_raw`
checks the zip and takes out raw_extractions.jsonl.gz.

`get_raw` puts the steps together and keeps everything under a cache folder,
so running the notebook again does not ask for a new link or download again.
"""

from __future__ import annotations

import datetime
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_BASE = "https://corporatedisclosuredata.org"
RAW_NAME = "raw_extractions.jsonl.gz"
PACKAGE_NAME = "cdd_raw_v1.0.0.zip"
USER_AGENT = "cdd-examples-notebook/1.0"

# The allowed answers, as on the site's download form
# (site/functions/lib/download_package.js: ROLES and USES).
ROLES = {
    "faculty": "Faculty",
    "doctoral_student": "Doctoral student",
    "other_student": "Other student",
    "journalist": "Journalist",
    "other": "Other",
}
USES = {
    "research": "Research",
    "teaching": "Teaching",
    "journalism": "Journalism",
    "other": "Other",
}
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)   # as on the site
LIMITS = {"name": 120, "email": 200, "institution": 200, "use_detail": 500, "heard": 200}


class DownloadError(Exception):
    """A problem the reader can act on; the message says what to do."""


# ------------------------------------------------------------------ answers

def choice(value, options, what):
    """Accept a key ("doctoral_student"), a label ("Doctoral student") or a number from the list."""
    text = str(value or "").strip()
    keys = list(options)
    if text.isdigit() and 1 <= int(text) <= len(keys):
        return keys[int(text) - 1]
    for key, label in options.items():
        if text.lower() in (key, label.lower(), key.replace("_", " ")):
            return key
    raise DownloadError(f"Choose a {what} from: " + ", ".join(f"{i}. {label}" for i, label in enumerate(options.values(), 1)))


def check_answers(answers):
    """Clean the answers and check them as the site does. Returns the JSON body to send."""
    out = {}
    for key in ("name", "email", "institution", "use_detail", "heard"):
        value = str(answers.get(key) or "").strip()
        if len(value) > LIMITS[key]:
            raise DownloadError(f"{key.replace('_', ' ').capitalize()} is too long (at most {LIMITS[key]} characters).")
        out[key] = value
    if not out["name"]:
        raise DownloadError("Enter your name.")
    if not EMAIL.match(out["email"]):
        raise DownloadError("Enter a valid email address.")
    out["role"] = choice(answers.get("role"), ROLES, "role")
    out["use"] = choice(answers.get("use"), USES, "use")
    if out["use"] == "other" and not out["use_detail"]:
        raise DownloadError("Say briefly how you will use the data.")
    if answers.get("agree") is not True:
        raise DownloadError("Confirm the Creative Commons Attribution-NonCommercial 4.0 International license (CC BY-NC 4.0).")
    out["news"] = bool(answers.get("news"))
    out["agree"] = True
    out["delivery"] = "direct"
    out["client"] = "colab"
    return out


# ------------------------------------------------------------------ request

def _error_text(body):
    try:
        message = json.loads(body).get("error")
    except (ValueError, AttributeError):
        message = None
    return message if isinstance(message, str) and message.strip() else None


def _https_or_local(url):
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1")
    return parts.scheme == "https" or (local and parts.scheme == "http")


def request_links(base, body, timeout=60):
    """POST the answers to {base}/api/download/request. Returns the reply: {ok, delivery, expiresAt, downloads}."""
    url = base.rstrip("/") + "/api/download/request"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "content-type": "application/json", "accept": "application/json", "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        message = _error_text(e.read().decode("utf-8", "replace"))
        if e.code == 400:
            raise DownloadError(f"The site did not accept the answers: {message or 'check them and try again.'}") from None
        if e.code == 429:
            wait = e.headers.get("retry-after")
            minutes = f" about {max(1, round(int(wait) / 60))} minutes" if wait and wait.isdigit() else " a while"
            raise DownloadError("Too many download requests from this address or email today. "
                                f"Wait{minutes} and run this cell again, or use the link from an earlier request "
                                "(set CDD_DOWNLOAD_URL).") from None
        if e.code == 503:
            raise DownloadError("Downloads are temporarily unavailable"
                                + (f" ({message})" if message else "")
                                + ". Try again later, or download the file from the site's download form and set RAW_PATH.") from None
        if e.code == 403:
            raise DownloadError("The site refused the request"
                                + (f": {message}" if message else "")
                                + ". Use the download form at https://corporatedisclosuredata.org/#download and set RAW_PATH "
                                  "or CDD_DOWNLOAD_URL instead.") from None
        raise DownloadError(f"The site answered with HTTP {e.code}" + (f": {message}" if message else "") + ".") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DownloadError(f"Could not reach {base}: {getattr(e, 'reason', e)}. Check the internet connection.") from None
    try:
        reply = json.loads(raw)
    except ValueError:
        raise DownloadError("The site's reply was not readable. Try again later.") from None
    downloads = reply.get("downloads") if isinstance(reply, dict) else None
    if not (isinstance(reply, dict) and reply.get("ok") is True and isinstance(downloads, list) and downloads):
        raise DownloadError("The site recorded the request but did not return a link. "
                            "If an email arrives, set CDD_DOWNLOAD_URL to its link and run this cell again.")
    for item in downloads:
        if not (isinstance(item, dict) and isinstance(item.get("url"), str) and isinstance(item.get("filename"), str)
                and _https_or_local(item["url"])):
            raise DownloadError("The site returned a link this notebook does not recognise. Try again later.")
    return reply


# ------------------------------------------------------------------ download

def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


def download(url, dest, timeout=60, chunk=1 << 20, quiet=False):
    """Save url to dest (via dest.part), printing progress. Returns dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res, open(part, "wb") as out:
            total = int(res.headers.get("content-length") or 0)
            done, start, shown = 0, time.time(), 0.0
            while True:
                block = res.read(chunk)
                if not block:
                    break
                out.write(block)
                done += len(block)
                now = time.time()
                if not quiet and (now - shown > 0.5 or (total and done == total)):
                    shown = now
                    rate = done / max(now - start, 1e-6)
                    pct = f"{100 * done / total:5.1f}% of {_size(total)}" if total else _size(done)
                    print(f"\r  downloading {dest.name}: {pct} ({_size(rate)}/s)   ", end="", flush=True)
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        if e.code in (400, 403):
            raise DownloadError("The download link was refused; links expire 24 hours after the request. "
                                "Clear CDD_DOWNLOAD_URL (or delete cdd_cache/last_request.json) and request a new one.") from None
        raise DownloadError(f"The download failed with HTTP {e.code}. Run the cell again to retry.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise DownloadError(f"The download stopped: {getattr(e, 'reason', e)}. Run the cell again to retry.") from None
    if total and done != total:
        part.unlink(missing_ok=True)
        raise DownloadError(f"The download stopped early ({_size(done)} of {_size(total)}). Run the cell again to retry.")
    if not quiet:
        print()
    part.replace(dest)
    return dest


def check_raw(path):
    """Raise DownloadError unless path is a readable gzip of JSON lines."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            first = json.loads(f.readline())
    except (OSError, ValueError, EOFError) as e:
        raise DownloadError(f"{path} is not the raw model extraction file (a gzip of JSON lines): {e}") from None
    if not isinstance(first, dict) or "model_output" not in first:
        raise DownloadError(f"{path} does not look like the raw model extraction (no model_output in the first record).")


def unpack_raw(zip_path, out_dir):
    """Check the zip, extract raw_extractions.jsonl.gz (plus README and LICENSE) into out_dir. Returns the raw path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
            if RAW_NAME not in names:
                raise DownloadError(f"{zip_path} has no {RAW_NAME}; it contains {sorted(names)}.")
            for name in (RAW_NAME, "README.md", "LICENSE"):
                if name in names:
                    target = out_dir / name
                    with z.open(names[name]) as src, open(str(target) + ".part", "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)   # the zip checks each file's CRC as it is read
                    Path(str(target) + ".part").replace(target)
    except zipfile.BadZipFile as e:
        raise DownloadError(f"{zip_path} is not a complete zip file ({e}). Delete it and run the cell again.") from None
    raw = out_dir / RAW_NAME
    check_raw(raw)
    return raw


# ------------------------------------------------------------------ all steps

def get_raw(cache_dir="cdd_cache", raw_path=None, download_url=None, answers=None, base=DEFAULT_BASE, ask=None):
    """Return the path of raw_extractions.jsonl.gz, getting it the cheapest way available.

    In order: raw_path (a .jsonl.gz or the .zip), a cached copy, download_url (a
    link from an earlier request or email), a still-valid earlier request, and
    last a new request with the answers. `ask` is called with no arguments to
    collect the answers only when a new request is needed.
    """
    cache = Path(cache_dir)
    raw_dir = cache / "raw"
    cached = raw_dir / RAW_NAME
    marker = raw_dir / ".checked"
    if raw_path:
        p = Path(os.path.expanduser(str(raw_path)))
        if not p.exists():
            raise DownloadError(f"RAW_PATH is set but {p} does not exist.")
        if p.suffix == ".zip":
            print(f"Unpacking {p} ...")
            return unpack_raw(p, raw_dir)
        check_raw(p)
        print(f"Using the raw model extraction file at {p}")
        return p
    if cached.exists() and marker.exists():
        print(f"Using the raw model extraction file already downloaded: {cached}")
        return cached
    if not download_url:
        last = cache / "last_request.json"
        if last.exists():
            try:
                reply = json.loads(last.read_text(encoding="utf-8"))
                expires = datetime.datetime.fromisoformat(reply["expiresAt"].replace("Z", "+00:00"))
                if expires - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(minutes=10):
                    download_url = reply["downloads"][0]["url"]
                    print(f"Reusing the link from the earlier request (valid until {reply['expiresAt']}).")
            except (ValueError, KeyError, IndexError, TypeError):
                pass
    if not download_url:
        body = check_answers(answers if answers is not None else ask())
        print(f"Sending your answers to {base} ...")
        reply = request_links(base, body)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "last_request.json").write_text(json.dumps(reply), encoding="utf-8")
        print(f"Got {len(reply['downloads'])} link(s), valid until {reply.get('expiresAt', 'in 24 hours')}.")
        download_url = reply["downloads"][0]["url"]
    name = Path(urllib.parse.urlsplit(download_url).path).name or PACKAGE_NAME
    target = cache / name
    download(download_url, target)
    if target.suffix == ".zip":
        print("Checking and unpacking the zip ...")
        raw = unpack_raw(target, raw_dir)
        target.unlink()                     # the unpacked file is kept; the zip is not needed again
    else:
        raw_dir.mkdir(parents=True, exist_ok=True)
        target.replace(cached)
        check_raw(cached)
        raw = cached
    marker.write_text(datetime.datetime.now(datetime.timezone.utc).isoformat(), encoding="utf-8")
    print(f"Saved {raw} ({_size(raw.stat().st_size)}).")
    return raw


if __name__ == "__main__":
    sys.exit("Import this module from the notebook (reproduce_figures.ipynb).")
