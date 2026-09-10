"""
Unescle dataset + image builder.

Pulls World Heritage Sites and Intangible Cultural Heritage elements from
Wikidata via SPARQL, downloads a freely-licensed thumbnail per entry from
Wikimedia Commons, and writes:

    data/dataset.json               -- all metadata (small, safe to load in full)
    data/noncommercial_images.json  -- entries whose only photo is CC BY-NC-*
    images/{id}.jpg                 -- one photo per entry (only today's 3 ever
                                       get requested by the front-end)

Design notes
------------
*One row per item.* Several OPTIONAL blocks in one query produce a cross
product: an item with three aliases per language, two images and six criteria
comes back as hundreds of identical-but-for-one-field rows. Multi-valued fields
are GROUP_CONCAT'd and single-valued ones SAMPLE'd, so each item returns exactly
one row -- which is what keeps a 250-item page from timing out.

*Pagination.* The spine of each query is an ORDER BY'd subquery with
LIMIT/OFFSET; the rest hangs off it. Pages are fetched one at a time with
backoff on 429/5xx.

*Coordinates are sampled as a pair.* An item with two P625 statements could
otherwise take its latitude from one and its longitude from the other, landing
the pin in the sea. lat and lon are concatenated before sampling.

*Countries are resolved once, in a batch.* Country labels, coordinates and
continents come from a single query over the distinct country QIDs rather than
riding along on every item row.

*Categorical fields are codes, not prose.* continent, category and type are
emitted as stable codes ("EU", "cultural", ...) and translated in unescle.html's
i18n table. Only per-entry text that genuinely varies -- names, country names,
descriptions -- is translated here. That guarantees the clue tiles read
correctly in all three languages even where Wikidata's fr/es coverage is thin,
and keeps dataset.json small.

*Translation honesty.* Where a fr/es label or description is missing we fall
back to English, but the fallback is counted and reported per field per
language, and the totals are written into dataset.json's `meta` block. A run
that quietly ships a mostly-English "trilingual" dataset should be visible.

Install: pip install requests --break-system-packages
Run:     python build_dataset.py
         python build_dataset.py --limit 40           # smoke test
         python build_dataset.py --skip-images        # metadata only
         python build_dataset.py --fixture tests/fixtures  # offline, no network
"""
import argparse
import functools
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote

import requests

print = functools.partial(print, flush=True)  # keep stdout ordered against stderr

# Wikimedia requires a descriptive User-Agent with contact info or it will
# start rejecting requests. Override with UNESCLE_CONTACT once the repo is public.
CONTACT = os.environ.get("UNESCLE_CONTACT", "paulbenard01@gmail.com")
HEADERS = {"User-Agent": f"Unescle/1.0 ({CONTACT}; personal heritage guessing game)"}

SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Heritage designations (P1435 values).
#   Q9259    -- World Heritage Site
#   Q1459900 -- Intangible Cultural Heritage element
# A handful of items carry both designations; the first one wins (see main()).
# If a run returns far fewer than `expected`, the designation item is probably
# wrong -- check what P1435 actually points at on a known entry.
DESIGNATIONS = [
    ("Q9259", "material", 1273),
    ("Q1459900", "immaterial", 849),
]

PAGE_SIZE = 250
ALIASES_PER_LANG = 4
IMAGE_WIDTH = 640          # photo box renders at <=480 CSS px; 640 covers retina
REQUEST_TIMEOUT = 90
MAX_RETRIES = 5
COMMONS_DELAY = 0.4        # be polite; raise if Commons starts 429ing
SEP = "|~|"                # GROUP_CONCAT separator, unlikely inside a label

# Non-commercial / non-free licence markers, matched case-insensitively against
# Commons' LicenseShortName. Entries matching these are flagged, never dropped.
NONCOMMERCIAL_PATTERNS = [
    r"\bnc\b", r"non-?commercial", r"\bfair use\b", r"non-?free", r"\bcc[- ]by[- ]nc",
]

# ---------------------------------------------------------------------------
# SPARQL
# ---------------------------------------------------------------------------

QUERY_TEMPLATE = """
SELECT ?item
       (SAMPLE(?labelEn_) AS ?labelEn)
       (SAMPLE(?labelFr_) AS ?labelFr)
       (SAMPLE(?labelEs_) AS ?labelEs)
       (SAMPLE(?descEn_)  AS ?descEn)
       (SAMPLE(?descFr_)  AS ?descFr)
       (SAMPLE(?descEs_)  AS ?descEs)
       (SAMPLE(?coord_)   AS ?coord)
       (SAMPLE(?sitelinks_) AS ?sitelinks)
       (SAMPLE(?inscribed_) AS ?inscribed)
       (GROUP_CONCAT(DISTINCT ?country_;     separator="%(sep)s") AS ?country)
       (GROUP_CONCAT(DISTINCT ?continentEn_; separator="%(sep)s") AS ?continentEn)
       (GROUP_CONCAT(DISTINCT ?image_;       separator="%(sep)s") AS ?image)
       (GROUP_CONCAT(DISTINCT ?criterionEn_; separator="%(sep)s") AS ?criterionEn)
WHERE {
  {
    SELECT ?item WHERE { ?item p:P1435/ps:P1435 wd:%(qid)s . }
    ORDER BY ?item
    LIMIT %(limit)d
    OFFSET %(offset)d
  }
  OPTIONAL { ?item rdfs:label ?labelEn_ . FILTER(lang(?labelEn_)="en") }
  OPTIONAL { ?item rdfs:label ?labelFr_ . FILTER(lang(?labelFr_)="fr") }
  OPTIONAL { ?item rdfs:label ?labelEs_ . FILTER(lang(?labelEs_)="es") }
  OPTIONAL { ?item schema:description ?descEn_ . FILTER(lang(?descEn_)="en") }
  OPTIONAL { ?item schema:description ?descFr_ . FILTER(lang(?descFr_)="fr") }
  OPTIONAL { ?item schema:description ?descEs_ . FILTER(lang(?descEs_)="es") }
  OPTIONAL { ?item wdt:P18 ?image_ . }
  OPTIONAL { ?item wikibase:sitelinks ?sitelinks_ . }
  OPTIONAL { ?item wdt:P17 ?country_ . }
  OPTIONAL { ?item p:P1435 [ ps:P1435 wd:%(qid)s ; pq:P580 ?inscribed_ ] . }
  # lat and lon are paired before sampling: an item with two coordinate
  # statements must not mix the latitude of one with the longitude of the other.
  OPTIONAL {
    ?item p:P625/psv:P625 [ wikibase:geoLatitude ?lat_ ; wikibase:geoLongitude ?lon_ ] .
    BIND(CONCAT(STR(?lat_), ",", STR(?lon_)) AS ?coord_)
  }
  OPTIONAL {
    ?item wdt:P30 ?continent_ .
    ?continent_ rdfs:label ?continentEn_ . FILTER(lang(?continentEn_)="en")
  }
  OPTIONAL {
    ?item wdt:P2614 ?criterion_ .
    ?criterion_ rdfs:label ?criterionEn_ . FILTER(lang(?criterionEn_)="en")
  }
}
GROUP BY ?item
"""

# Aliases feed the autocomplete. Kept out of the main query because three
# languages of multi-valued altLabel is the worst cross-product offender.
ALIAS_QUERY = """
SELECT ?item ?alt WHERE {
  VALUES ?item { %s }
  ?item skos:altLabel ?alt .
  FILTER(lang(?alt) IN ("en", "fr", "es"))
}
"""

# Everything country-shaped in one batched query: names for the lookup table,
# a coordinate for entries that have none of their own (intangible elements are
# practices rather than places, so most lack P625), and a continent fallback.
COUNTRY_INFO_QUERY = """
SELECT ?country ?cEn ?cFr ?cEs ?coord ?contEn WHERE {
  VALUES ?country { %s }
  OPTIONAL { ?country rdfs:label ?cEn . FILTER(lang(?cEn)="en") }
  OPTIONAL { ?country rdfs:label ?cFr . FILTER(lang(?cFr)="fr") }
  OPTIONAL { ?country rdfs:label ?cEs . FILTER(lang(?cEs)="es") }
  OPTIONAL {
    ?country p:P625/psv:P625 [ wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon ] .
    BIND(CONCAT(STR(?lat), ",", STR(?lon)) AS ?coord)
  }
  OPTIONAL { ?country wdt:P30 ?cont . ?cont rdfs:label ?contEn . FILTER(lang(?contEn)="en") }
}
"""


class Fixtures:
    """Offline replay of recorded SPARQL/Commons responses (--fixture)."""

    def __init__(self, path):
        self.path = path

    def _load(self, name):
        f = os.path.join(self.path, name)
        if not os.path.exists(f):
            return None
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)

    def sparql(self, query):
        if "skos:altLabel" in query:
            return self._load("aliases.json") or {"results": {"bindings": []}}
        if "VALUES ?country" in query:
            return self._load("country_info.json") or {"results": {"bindings": []}}
        qid = re.search(r"wd:(Q\d+)", query)
        if int(re.search(r"OFFSET (\d+)", query).group(1)) > 0:
            return {"results": {"bindings": []}}  # fixtures are a single page
        return self._load(f"{qid.group(1)}.json") or {"results": {"bindings": []}}

    def imageinfo(self, filename):
        return (self._load("commons.json") or {}).get(filename)


FIXTURES = None


def sparql(query):
    """POST a SPARQL query, retrying on rate limits and transient failures."""
    if FIXTURES:
        return FIXTURES.sparql(query)["results"]["bindings"]

    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(
                SPARQL_URL,
                data={"query": query, "format": "json"},
                headers={**HEADERS, "Accept": "application/sparql-results+json"},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                wait = int(r.headers.get("Retry-After") or 2 ** (attempt + 2))
                print(f"    WDQS {r.status_code}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except (requests.RequestException, ValueError) as exc:
            wait = 2 ** (attempt + 2)
            print(f"    WDQS error ({exc}), retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("WDQS failed after retries -- try a smaller PAGE_SIZE")


def val(row, key):
    """Read a SPARQL binding, returning None when the optional didn't match."""
    cell = row.get(key)
    if not cell:
        return None
    v = cell.get("value")
    return v if v not in ("", None) else None


def multi(row, key):
    """Split a GROUP_CONCAT'd binding back into a list."""
    v = val(row, key)
    return [p for p in v.split(SEP) if p] if v else []


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


def parse_coord(s):
    """'12.5,-3.25' -> (12.5, -3.25); None when absent or malformed."""
    if not s:
        return None
    try:
        lat, lon = s.split(",")
        lat, lon = float(lat), float(lon)
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# ---------------------------------------------------------------------------
# Field derivation
# ---------------------------------------------------------------------------

CONTINENT_CODES = {
    "europe": "EU", "asia": "AS", "africa": "AF", "north america": "NA",
    "south america": "SA", "oceania": "OC", "australia": "OC",
    "antarctica": "AN", "insular oceania": "OC", "americas": "NA",
    "eurasia": "EU", "australian continent": "OC",
}

CULTURAL_CRITERIA = {"i", "ii", "iii", "iv", "v", "vi"}
NATURAL_CRITERIA = {"vii", "viii", "ix", "x"}


def continent_code(labels):
    for label in labels:
        code = CONTINENT_CODES.get((label or "").strip().lower())
        if code:
            return code
    return None


def category_code(kind, criteria):
    """cultural / natural / mixed for sites, from the roman numeral in each
    criterion's English label. Avoids hardcoding criterion QIDs."""
    if kind == "immaterial":
        return "intangible"
    found = set()
    for label in criteria:
        m = re.search(r"\(([ivx]+)\)", (label or "").lower())
        if m:
            found.add(m.group(1))
    has_c, has_n = bool(found & CULTURAL_CRITERIA), bool(found & NATURAL_CRITERIA)
    if has_c and has_n:
        return "mixed"
    if has_n:
        return "natural"
    if has_c:
        return "cultural"
    return "site"


def slugify(name, qid):
    """Deterministic, ASCII, collision-free id.

    Disambiguation uses the QID rather than an incrementing counter so an id
    never depends on iteration order -- ids are filenames under images/ and
    localStorage keys, so they must not shuffle between runs.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return base[:60] or qid.lower()


def is_noncommercial(license_name):
    lic = (license_name or "").lower()
    return any(re.search(p, lic) for p in NONCOMMERCIAL_PATTERNS)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_aliases(qids):
    """altLabels for one page of items, capped per language."""
    if not qids:
        return {}
    out = defaultdict(lambda: defaultdict(list))
    values = " ".join(f"wd:{q}" for q in qids)
    for row in sparql(ALIAS_QUERY % values):
        q = qid_of(val(row, "item"))
        cell = row.get("alt") or {}
        lang, text = cell.get("xml:lang"), cell.get("value")
        if q and lang and text and len(out[q][lang]) < ALIASES_PER_LANG:
            out[q][lang].append(text)
    return out


def fetch_designation(qid, kind, limit=None):
    """Page through one designation. The query returns one row per item."""
    items = {}
    offset = 0
    page_size = min(PAGE_SIZE, limit) if limit else PAGE_SIZE

    while True:
        rows = sparql(QUERY_TEMPLATE % {"qid": qid, "limit": page_size,
                                        "offset": offset, "sep": SEP})
        if not rows:
            break

        page = {}
        for row in rows:
            q = qid_of(val(row, "item"))
            if not q:
                continue
            it = {
                "qid": q,
                "type": kind,
                "names": {l: val(row, f"label{l.title()}") for l in ("en", "fr", "es")},
                "desc": {l: val(row, f"desc{l.title()}") for l in ("en", "fr", "es")},
                "sitelinks": val(row, "sitelinks"),
                "inscribed": val(row, "inscribed"),
                "country_qid": qid_of(multi(row, "country")[0]) if multi(row, "country") else None,
                "continent_labels": multi(row, "continentEn"),
                "criteria": multi(row, "criterionEn"),
                "images": multi(row, "image"),
                "aliases": {"en": [], "fr": [], "es": []},
            }
            coord = parse_coord(val(row, "coord"))
            if coord:
                it["lat"], it["lng"] = coord
            page[q] = it

        aliases = fetch_aliases(list(page))
        for q, it in page.items():
            for lang, vals in aliases.get(q, {}).items():
                it["aliases"][lang] = vals

        items.update(page)
        offset += page_size
        print(f"  {kind}: {len(items)} items after offset {offset}")
        if limit and len(items) >= limit:
            break
        if len(rows) < page_size:
            break
    return items


def resolve_countries(items):
    """Batch-resolve every referenced country: names, centroid, continent."""
    qids = sorted({it["country_qid"] for it in items.values() if it.get("country_qid")})
    info = {}
    if not qids:
        return info
    # VALUES lists of a few hundred are fine in one query.
    for chunk in (qids[i:i + 400] for i in range(0, len(qids), 400)):
        values = " ".join(f"wd:{q}" for q in chunk)
        for row in sparql(COUNTRY_INFO_QUERY % values):
            q = qid_of(val(row, "country"))
            if not q:
                continue
            rec = info.setdefault(q, {})
            for key, src in (("en", "cEn"), ("fr", "cFr"), ("es", "cEs")):
                if val(row, src):
                    rec.setdefault(key, val(row, src))
            if val(row, "coord") and "coord" not in rec:
                rec["coord"] = parse_coord(val(row, "coord"))
            if val(row, "contEn") and "continent" not in rec:
                rec["continent"] = val(row, "contEn")
    print(f"  resolved {len(info)}/{len(qids)} countries")

    filled_coord = filled_cont = 0
    for it in items.values():
        rec = info.get(it.get("country_qid")) or {}
        if "lat" not in it and rec.get("coord"):
            it["lat"], it["lng"] = rec["coord"]
            it["approx_location"] = True
            filled_coord += 1
        if not it["continent_labels"] and rec.get("continent"):
            it["continent_labels"] = [rec["continent"]]
            filled_cont += 1
    print(f"  filled {filled_coord} coordinates and {filled_cont} continents "
          f"from the linked country")
    return info


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def commons_imageinfo(filename):
    if FIXTURES:
        return FIXTURES.imageinfo(filename)
    for attempt in range(3):
        try:
            r = requests.get(COMMONS_API, params={
                "action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
                "iiprop": "url|extmetadata", "iiurlwidth": IMAGE_WIDTH, "format": "json",
            }, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After") or 5))
                continue
            r.raise_for_status()
            page = next(iter(r.json()["query"]["pages"].values()))
            info = (page.get("imageinfo") or [None])[0]
            if not info:
                return None
            meta = info.get("extmetadata", {})
            strip = lambda s: re.sub(r"<[^<]+?>", "", s or "").strip()
            return {
                "thumb_url": info.get("thumburl") or info.get("url"),
                "license": meta.get("LicenseShortName", {}).get("value", "unknown"),
                "artist": strip(meta.get("Artist", {}).get("value", "")),
                "credit": strip(meta.get("Credit", {}).get("value", "")),
                "descriptionurl": info.get("descriptionurl", ""),
            }
        except (requests.RequestException, ValueError, StopIteration, KeyError):
            time.sleep(2 ** (attempt + 1))
    return None


def download_image(entry, image_uris, out_dir="images"):
    """Attach the first usable Commons photo. Licence + attribution always ride
    along with the file -- the credit line in-game depends on it."""
    for uri in image_uris[:3]:
        filename = unquote(uri.split("/")[-1]).replace("_", " ")
        info = commons_imageinfo(filename)
        if not FIXTURES:
            time.sleep(COMMONS_DELAY)
        if not info or not info["thumb_url"]:
            continue
        path = f"{out_dir}/{entry['id']}.jpg"
        if FIXTURES:
            pass  # offline: exercise the metadata path without fetching bytes
        elif not os.path.exists(path):  # resumable: don't re-download on a rerun
            os.makedirs(out_dir, exist_ok=True)
            try:
                img = requests.get(info["thumb_url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
                img.raise_for_status()
                if not img.headers.get("Content-Type", "").startswith("image/"):
                    continue
                with open(path, "wb") as f:
                    f.write(img.content)
            except requests.RequestException:
                continue
        entry["image"] = {
            "path": path,
            "file": filename,
            "license": info["license"],
            "credit": info["artist"] or info["credit"] or "Wikimedia Commons",
            "source": info["descriptionurl"],
        }
        if is_noncommercial(info["license"]):
            entry["image"]["nonCommercial"] = True
        return True
    return False


# ---------------------------------------------------------------------------
# Tiering
# ---------------------------------------------------------------------------

def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def assign_tiers(entries, cuts=(1 / 3, 2 / 3)):
    """Tier 1 = widely recognized ... 3 = hidden gem.

    Split per type, and by *rank* rather than by sitelink value.

    Two things force this. First, material sites and intangible elements have
    very different sitelink spreads -- an intangible element with 12 sitelinks
    is famous, a World Heritage Site with 12 is obscure -- so one global cutoff
    would sweep nearly every intangible element into tier 3. Second, the
    distribution is long-tailed with a huge tie at the bottom: a third or more
    of entries sit on 0-1 sitelinks, so a value cutoff at the 33rd percentile
    lands *on* the minimum and "sitelinks < cut" matches nothing, leaving tier 3
    empty and the day's third round unfillable. Ranking is immune to that.

    Realised sitelink boundaries and the resulting counts are printed so the
    split can still be eyeballed against the real distribution.
    """
    by_type = defaultdict(list)
    for e in entries:
        by_type[e["type"]].append(e)

    for kind, group in sorted(by_type.items()):
        # Deterministic: qid breaks ties so a rerun tiers identically.
        group.sort(key=lambda e: (-e["sitelinks"], e["qid"]))
        n = len(group)
        c1, c2 = int(n * cuts[0]), int(n * cuts[1])
        for i, e in enumerate(group):
            e["tier"] = 1 if i < c1 else (2 if i < c2 else 3)

        vals = sorted(e["sitelinks"] for e in group)
        b1 = group[c1 - 1]["sitelinks"] if c1 else "-"
        b2 = group[c2 - 1]["sitelinks"] if c2 else "-"
        counts = Counter(e["tier"] for e in group)
        print(f"  {kind}: n={n} min={vals[0]} median={percentile(vals, 50):.0f} max={vals[-1]}")
        print(f"    tier1 >= {b1} sitelinks | tier2 >= {b2} | tier3 below that")
        print(f"    tier1={counts[1]} tier2={counts[2]} tier3={counts[3]}")
        if b1 == b2:
            print(f"    NOTE: {kind} tier boundaries fall on the same sitelink count "
                  f"({b1}); the tier-2/3 split there is arbitrary.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def to_entry(it, seen_ids, coverage):
    names = {l: v for l, v in it["names"].items() if v}
    name_en = names.get("en") or names.get("fr") or names.get("es")
    if not name_en or "lat" not in it:
        return None  # unusable: no name at all, or no coordinate even after fallback

    slug = slugify(name_en, it["qid"])
    if slug in seen_ids:
        slug = f"{slug}-{it['qid'].lower()}"
    seen_ids.add(slug)

    desc = {l: v for l, v in it["desc"].items() if v}
    entry = {
        "id": slug,
        "qid": it["qid"],
        "type": it["type"],
        "names": {},
        "lat": round(it["lat"], 4),
        "lng": round(it["lng"], 4),
        "sitelinks": int(it.get("sitelinks") or 0),
        "continent": continent_code(it["continent_labels"]),
        "category": category_code(it["type"], it["criteria"]),
    }

    for lang in ("en", "fr", "es"):
        entry["names"][lang] = names.get(lang) or name_en
        coverage["names"][lang] += 1 if names.get(lang) else 0
        if desc.get(lang):
            coverage["desc"][lang] += 1
    if desc:  # fall back to English so no language shows an empty clue
        fallback = desc.get("en") or next(iter(desc.values()))
        entry["desc"] = {l: desc.get(l) or fallback for l in ("en", "fr", "es")}

    if it.get("country_qid"):
        entry["country"] = it["country_qid"]
    if it.get("inscribed"):
        m = re.match(r"(-?\d{1,4})-", it["inscribed"])
        if m:
            entry["year"] = int(m.group(1))
    if it.get("approx_location"):
        entry["approx"] = True

    aliases = {a for vals in it["aliases"].values() for a in vals}
    aliases |= set(entry["names"].values())
    entry["aliases"] = sorted({a for a in aliases if a})[:10]
    return entry


def main():
    global FIXTURES
    ap = argparse.ArgumentParser(description="Build the Unescle dataset.")
    ap.add_argument("--limit", type=int, help="cap items per designation (smoke test)")
    ap.add_argument("--skip-images", action="store_true", help="metadata only")
    ap.add_argument("--fixture", help="replay recorded responses from this dir (offline)")
    ap.add_argument("--out", default="data/dataset.json")
    args = ap.parse_args()

    if args.fixture:
        FIXTURES = Fixtures(args.fixture)
        print(f"OFFLINE: replaying fixtures from {args.fixture}")

    all_items, dual = {}, []
    for qid, kind, expected in DESIGNATIONS:
        print(f"\nFetching {kind} ({qid})...")
        items = fetch_designation(qid, kind, limit=args.limit)
        print(f"  {kind}: {len(items)} distinct items")
        if not args.limit and not args.fixture and len(items) < expected * 0.5:
            print(f"  WARNING: expected ~{expected}, got {len(items)}. "
                  f"Check that {qid} is the right P1435 value.", file=sys.stderr)
        # A few items hold both designations. Keep the first and say so, rather
        # than letting the second silently overwrite it and mislabel its type.
        for q, it in items.items():
            if q in all_items:
                dual.append((q, all_items[q]["type"], kind))
            else:
                all_items[q] = it
    if dual:
        print(f"\n{len(dual)} item(s) carry both designations; kept the first:")
        for q, kept, skipped in dual[:10]:
            print(f"  {q}: kept {kept}, skipped {skipped}")

    print("\nResolving countries...")
    countries = resolve_countries(all_items)

    coverage = {"names": Counter(), "desc": Counter()}
    seen_ids, dataset = set(), []
    no_name = no_coord = 0
    for it in sorted(all_items.values(), key=lambda i: i["qid"]):
        entry = to_entry(it, seen_ids, coverage)
        if entry:
            dataset.append(entry)
        elif not any(it["names"].values()):
            no_name += 1
        else:
            no_coord += 1
    print(f"\n{len(dataset)} usable entries from {len(all_items)} items "
          f"({no_name} dropped for no name, {no_coord} for no coordinate)")

    print("\nFame tiers (rank tertiles within each type):")
    assign_tiers(dataset)

    if not args.skip_images:
        print("\nDownloading photos...")
        for i, entry in enumerate(dataset, 1):
            images = all_items[entry["qid"]]["images"]
            if images:
                download_image(entry, images)
            if i % 50 == 0:
                got = sum(1 for e in dataset[:i] if "image" in e)
                print(f"  {i}/{len(dataset)} processed, {got} photos")

    dataset.sort(key=lambda e: -e["sitelinks"])  # fame-ranked, highest first

    country_table = {
        q: {"en": r.get("en") or q,
            "fr": r.get("fr") or r.get("en") or q,
            "es": r.get("es") or r.get("en") or q}
        for q, r in countries.items() if r.get("en") or r.get("fr") or r.get("es")
    }

    n = len(dataset) or 1
    meta = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(dataset),
        "countsByType": dict(Counter(e["type"] for e in dataset)),
        "withPhoto": sum(1 for e in dataset if "image" in e),
        "approxCoordinates": sum(1 for e in dataset if e.get("approx")),
        "nonCommercialPhotos": sum(1 for e in dataset if e.get("image", {}).get("nonCommercial")),
        "missingContinent": sum(1 for e in dataset if not e.get("continent")),
        "translationCoverage": {
            field: {lang: round(100 * coverage[field][lang] / n, 1) for lang in ("en", "fr", "es")}
            for field in ("names", "desc")
        },
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "countries": country_table, "entries": dataset},
                  f, ensure_ascii=False, separators=(",", ":"))

    nc = [{"id": e["id"], "name": e["names"]["en"], "license": e["image"]["license"],
           "file": e["image"]["file"]}
          for e in dataset if e.get("image", {}).get("nonCommercial")]
    nc_path = os.path.join(os.path.dirname(args.out) or ".", "noncommercial_images.json")
    with open(nc_path, "w", encoding="utf-8") as f:
        json.dump(nc, f, ensure_ascii=False, indent=1)

    cov = meta["translationCoverage"]
    print(f"\nWrote {len(dataset)} entries to {args.out}")
    print(f"  photos: {meta['withPhoto']}  |  country-centroid coords: "
          f"{meta['approxCoordinates']}  |  no continent: {meta['missingContinent']}")
    print(f"  name coverage:  en {cov['names']['en']}%  fr {cov['names']['fr']}%  es {cov['names']['es']}%")
    print(f"  descr coverage: en {cov['desc']['en']}%  fr {cov['desc']['fr']}%  es {cov['desc']['es']}%")
    if cov["names"]["fr"] < 60 or cov["names"]["es"] < 60:
        print("  WARNING: a large share of entries fall back to the English name in "
              "fr/es. Worth flagging before shipping as 'trilingual'.", file=sys.stderr)
    if nc:
        print(f"  {len(nc)} photos are non-commercial-only -- listed in {nc_path} (kept, not dropped)")


if __name__ == "__main__":
    main()
