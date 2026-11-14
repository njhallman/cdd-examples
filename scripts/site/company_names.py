"""Company names for display on the homepage charts (stdlib only).

The raw file carries each company's EDGAR "conformed" name, which for many
companies is in capitals ("JPMORGAN CHASE & CO", "MCKESSON CORP /DE/"). The
rules, in order:

1. The EDGAR state tag at the end is removed: "/DE/", "/DE", "\\DE\\", "/ MA",
   "(DE)", "/NEW/", and so is a trailing "/" ("METTLER TOLEDO INTERNATIONAL INC/").
2. A name filed in mixed case is kept as filed ("Apple Inc."). A name counts as
   filed in capitals when fewer than a quarter of its letters are lower case
   ("ELI LILLY & Co", "McGRATH RENTCORP").
3. A name filed in capitals keeps its own words, letters and punctuation; only
   their case changes. The SEC's own name for the company (`entityName` in the
   SEC XBRL data, data/site/sec_entity_names.csv) is used as a guide to casing
   when it is in mixed case (has a lower-case letter):
   * if its letters and digits are exactly those of the filed name (ignoring
     case, spaces and punctuation), each letter takes the case of the same
     letter in the SEC name ("CREDITRISKMONITOR COM INC" with
     "CreditRiskMonitor.com, Inc." gives "CreditRiskMonitor.com Inc");
   * otherwise a filed word takes the casing of a word in the SEC name with
     exactly the same letters ("UNITEDHEALTH GROUP INC" with "UnitedHealth
     Group Incorporated" gives "UnitedHealth Group Inc").
   Filed words that the SEC name joins with a hyphen are joined the same way
   ("ELECTRO SENSORS" with "Electro-Sensors"; "GORMAN RUPP" with "The
   Gorman-Rupp Company"). The SEC name never adds or removes a word or letter, so "Corp" is not
   lengthened to "Corporation" and no leading "The" is added. A guided word is
   not used when it has a run of four or more capitals ("McGRATH", "PROCTER"),
   and legal endings always follow LEGAL ("Inc", "Corp"); such words, and words
   with no guide, are re-cased by rule 4.
4. Otherwise each word is re-cased:
   * brand spellings: the Ask page's list (CASE_TOKENS and the mixed-case words
     in COMPANY_DISPLAY, site/company_display.js) plus BRANDS below, except the
     ordinary words in GENERIC ("Bancorp" stays "Bancorp", not "BanCorp");
   * legal endings and acronyms in LEGAL ("Inc", "Corp", "PLC", "USA");
   * words of 2 to 4 letters with no vowel (Y counts as one) stay in capitals (CVS, PVH, TJX), and
     so do 2- and 3-letter words that are not in ORDINARY (AAR, ABM, MDU); also
     the 4-letter acronyms in ACRONYMS (CACI, AGCO);
   * single letters stay capitals (D R Horton); Roman numerals II to X too;
   * "Mc" names: McKesson, McCormick;
   * joining words after the first word are lower case (of, and, the);
   * "COM" between two words joins the first as ".com" (Amazon.com Inc);
   * ordinals and letters next to digits as before (1st Source, 3M, L3Harris);
   * everything else: first letter capital, rest lower case. Words joined by a
     hyphen, full stop, slash or "&" are cased part by part (Bio-Rad, La-Z-Boy).
5. A few brand names filed without their hyphen get it back (PHRASES):
   Coca-Cola, Kimberly-Clark, Mettler-Toledo, Archer-Daniels-Midland, G-III,
   CEL-SCI. These change only punctuation and case, never the words.
The output depends only on the name and these lists, so it is deterministic.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

STATE_TAG = re.compile(r"\s*(?:[/\\]\s*[A-Za-z]{2,4}\s*[/\\]?|\([A-Z]{2}\))\s*$")

# Ordinary words the Ask list spells as brands; here they are plain words.
GENERIC = {
    "bancorp", "bancshares", "biomed", "biomedical", "biopharm", "biopharma", "biopharmaceuticals", "bioscience",
    "biosciences", "biosolutions", "biotherapeutics", "blockchain", "encore", "healthcare", "homecare", "hometown",
    "image", "immunotech", "immunotherapeutics", "lifescience", "lifesciences", "lifestyle", "medtech", "microsystems",
    "northeast", "northwestern", "uti", "xcel", "fintech", "globaltech", "goldmining", "northstar",
    # the Ask page's own skip list (CASE_SKIP in site/present.js)
    "therapeutics", "pharmaceuticals", "technologies", "technology", "holdings", "systems", "energy", "international",
    "financial", "materials", "sciences", "science", "molecular", "communications", "semiconductors", "semiconductor",
    "solutions", "services", "products", "resources", "industries", "properties", "entertainment", "software",
    "networks", "health",
}
BRANDS = {"jpmorgan": "JPMorgan", "metlife": "MetLife", "idexx": "IDEXX", "1stdibs": "1stDibs", "wafd": "WaFd",
          "ebay": "eBay", "iheartmedia": "iHeartMedia"}
LEGAL = {"inc": "Inc", "corp": "Corp", "co": "Co", "ltd": "Ltd", "plc": "PLC", "llc": "LLC", "lp": "LP", "llp": "LLP",
         "nv": "NV", "sa": "SA", "ag": "AG", "se": "SE", "asa": "ASA", "reit": "REIT", "etf": "ETF", "usa": "USA",
         "us": "US", "adr": "ADR", "bdc": "BDC", "spac": "SPAC"}
ACRONYMS = {"aaon", "abvc", "acnb", "adma", "agco", "caci", "cbre", "ceco", "copt", "gatx", "idex", "imax",
            "mbia", "mgic", "opko", "pamt", "uscb"}
PHRASES = {"Coca Cola": "Coca-Cola", "Kimberly Clark": "Kimberly-Clark", "Mettler Toledo": "Mettler-Toledo",
           "Archer Daniels Midland": "Archer-Daniels-Midland", "G III": "G-III", "Cel SCI": "CEL-SCI"}
ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "x"}
SMALL = {"of", "and", "the", "for", "in", "or", "a", "an", "at", "by", "to", "de", "du", "la", "le"}
# Two- and three-letter words that are ordinary words, names or brands written in
# ordinary case; any other 2- or 3-letter word in a capitalised name is read as an
# acronym and kept in capitals.
ORDINARY = set("""
ad air al all am an and ann any ben dan eli hal ian jim kay lee rex roy sam ted tom ape app arc are ark art ash ask at ate aid ave awe axe bad bag ban bar bay be bed bee bel
big bin bio bit bob bow box boy bud bug bus but buy by cab cal cam can cap car cat cel cup cut dam day del den dew dex
did dig dim do doc dog don dot dow dr dry du due duo dye ear eat eco ed eel egg ego el elk elm end era eve eye fab
fan far fat fax fed fee few fig fin fir fit fix fly foe for fox fry fun fur gap gas gel gem geo get go god got gro gum
gun gut guy gym had ham has hat hay he hen her hex hey him hip his hit hog hop hot how hub hue hug hut ice ink inn
ion is its ivy jam jar jaw jay jet jig job joe jog joy jr jug key kid kin kit la lab lad lag lam las law lay leg let
lid lie lip lit log lot low lux mad man map mar mat max may me med men met mid mix mob mom mop mr mrs ms mt mud mug
my nap net new nil nod nor not now nu nut oak oar oat odd off oil old one opt orb ore our out owe owl own pad pal
pan par pat paw pay pea peg pen pep per pet pie pig pin pit ply pod pop pot pro pub pug pun pup put rad rag ram ran rap
rat raw ray re red rib rid rig rim rip rob rod rot row rub rug rum run rye sad sag san sat saw say sea see set sew
she shy sin sip sir sit six ski sky sly so sod son sow soy spa spy sr st sub sue sum sun tab tag tan tap tar tax tea
tek ten the thy tie tin tip toe ton too top tow toy try tub tug two up ur van vet via vie vim vow wag war was wax way we
web wed wet who why wig win wit wok won woo wow yak yam yes yet yo you yum zag zap zen zig zip zoo
""".split())


def load_ask_tokens(path):
    """Brand spellings from the Ask page's site/company_display.js, lower case -> spelling."""
    consts = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"export const (\w+) = (.*);$", line)
        if m:
            consts[m.group(1)] = json.loads(m.group(2))
    tokens = {}
    for value in consts.get("COMPANY_DISPLAY", {}).values():
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", value):
            if re.search(r"[a-z]", w) and re.search(r"[A-Z]", w[1:]):
                tokens.setdefault(w.lower(), w)
    tokens.update(consts.get("CASE_TOKENS", {}))
    return tokens


def load_entity_names(path):
    names = {}
    if path and Path(path).exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                names[str(int(row["cik"]))] = row["entity_name"]
    return names


def recase_like(text, model):
    """text with each letter in the case of the matching letter of model (same letters and digits, in order)."""
    letters = iter(c for c in model if c.isalnum())
    return "".join(next(letters) if c.isalnum() else c for c in text)


def alnum(text):
    return "".join(c for c in text.lower() if c.isalnum())


def hyphens_like(filed, sec):
    """Join filed words with a hyphen where the SEC name hyphenates the same words ("ELECTRO SENSORS" with
    "Electro-Sensors" gives "ELECTRO-SENSORS"); nothing else changes."""
    for w in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", sec):
        parts = w.split("-")
        filed = re.sub(r"(?i)(?<![A-Za-z0-9])" + " ".join(map(re.escape, parts)) + r"(?![A-Za-z0-9])",
                       lambda m: m.group(0).replace(" ", "-"), filed)
    return filed


def case_guides(filed, sec):
    """For each word of the filed name, the same word in the SEC name's casing, or None.

    The whole SEC name is used when its letters and digits equal the filed name's;
    otherwise each filed word is matched to an SEC word with the same letters (a
    word spelt in two different casings in the SEC name is not used).
    """
    words = filed.split(" ")
    if alnum(filed) == alnum(sec):
        cased = recase_like(filed, sec).split(" ")
        return [c if alnum(w) else None for w, c in zip(words, cased)]
    by_key = {}
    for w in sec.split():
        k = alnum(w)
        if k:
            by_key.setdefault(k, set()).add("".join(c for c in w if c.isalnum()))
    guides = []
    for w in words:
        k = alnum(w)
        g = by_key.get(k) if k else None
        guides.append(recase_like(w, next(iter(g))) if g and len(g) == 1 else None)
    return guides


class Namer:
    def __init__(self, display_js=None, entity_names_csv=None):
        tokens = load_ask_tokens(display_js) if display_js and Path(display_js).exists() else {}
        tokens.update(BRANDS)
        self.tokens = {k: v for k, v in tokens.items() if k not in GENERIC}
        self.entity = load_entity_names(entity_names_csv)

    def __call__(self, name, cik=None):
        s = re.sub(r"\s*[/\\]+$", "", STATE_TAG.sub("", re.sub(r"\s+", " ", name).strip()).strip())
        letters = [c for c in s if c.isalpha()]
        if sum(c.islower() for c in letters) * 4 >= len(letters):
            return s
        sec = self.entity.get(str(cik)) if cik is not None else None
        guides = None
        if sec:
            sec = STATE_TAG.sub("", re.sub(r"\s+", " ", sec).strip()).strip()
            if any(c.islower() for c in sec):
                s = hyphens_like(s, sec)
                guides = case_guides(s, sec)
        return self.recase(s, guides)

    def recase(self, s, guides=None):
        """Re-case word by word; guides[i], when usable, is word i in the SEC name's casing."""
        words = s.split(" ")
        out = []
        for i, w in enumerate(words):
            if (w.upper() == "COM" and 0 < i < len(words) - 1 and out):
                out[-1] += ".com"
                continue
            g = guides[i] if guides else None
            key = re.sub(r"[^a-z0-9]", "", w.lower())
            if g and not re.search(r"[A-Z]{4,}", g) and key not in LEGAL:
                if i == 0 and g[:1].islower() and key in SMALL:
                    g = g[0].upper() + g[1:]
                out.append(g)
            else:
                out.append(self.word(w, first=(i == 0)))
        text = " ".join(out)
        for plain, brand in PHRASES.items():
            text = re.sub(r"\b%s\b" % plain, brand, text)
        return text

    def word(self, w, first):
        whole = w.strip(".,").lower()
        if whole in self.tokens and whole not in LEGAL:
            return w.lower().replace(whole, self.tokens[whole], 1)
        if "&" in w and all(len(p) <= 3 for p in re.findall(r"[A-Za-z]+", w)):
            return w                                            # AT&T, S&T, B&G
        if any(c.isdigit() for c in w):                         # 1ST -> 1st, L3HARRIS -> L3Harris, 3M stays
            w = re.sub(r"(?<=\d)(ST|ND|RD|TH)\b", lambda m: m.group(0).lower(), w)
            return re.sub(r"[A-Z]{3,}", lambda m: self.part(m.group(0), False), w)
        res = []
        for j, p in enumerate(re.split(r"([^A-Za-z]+)", w)):
            if re.fullmatch(r"[A-Za-z]+", p):
                after_quote = j > 0 and res[-1].endswith("'")
                after_dot = j > 0 and res[-1].endswith(".") and p.lower() == "com"
                res.append(p.lower() if (after_quote and len(p) == 1) or after_dot else self.part(p, first and j == 0))
            else:
                res.append(p)
        return "".join(res)

    def part(self, p, first):
        low = p.lower()
        if low in LEGAL:
            return LEGAL[low]
        if low in self.tokens:
            return self.tokens[low]
        if low in ROMAN and not first:
            return p.upper()
        if len(p) == 1:
            return p.upper()
        if not first and low in SMALL:
            return low
        if low in ACRONYMS:
            return p.upper()
        if low in ORDINARY:
            return p.capitalize()
        if re.fullmatch(r"[b-df-hj-np-tv-xz]{2,4}", low) or len(p) <= 3:
            return p.upper()
        if low.startswith("mc") and len(p) >= 5:
            return "Mc" + p[2:].capitalize()
        return p.capitalize()
