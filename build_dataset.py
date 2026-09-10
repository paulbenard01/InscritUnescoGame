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
*Pagination.* One query with six OPTIONAL blocks over ~1,300 items reliably
exceeds the 60s WDQS timeout, so the spine of each query is an ORDER BY'd
subquery with LIMIT/OFFSET and the OPTIONALs hang off that. Pages are fetched
one at a time with backoff.

*Row grouping.* Multiple OPTIONALs produce a cross product -- an item with two
images and two countries comes back as four rows. Rows are grouped by QID and
merged, rather than being treated as one entry each.

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
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote

import requests

# Wikimedia requires a descriptive User-Agent with contact info or it will
# start rejecting requests. Override with UNESCLE_CONTACT once the repo is public.
CONTACT = os.environ.get("UNESCLE_CONTACT", "paulbenard01@gmail.com")
HEADERS = {"User-Agent": f"Unescle/1.0 ({CONTACT}; personal heritage guessing game)"}

SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Heritage designations (P1435 values).
#   Q9259    -- World Heritage Site
#   Q1459900 -- Intangible Cultural Heritage element
# If a first live run returns a wildly different count than EXPECTED_ROWS below,
# the designation item is probably wrong -- check what P1435 actually points at
# on a known entry before trusting the output.
DESIGNATIONS = [
    ("Q9259", "material", 1273),
    ("Q1459900", "immaterial", 849),
]

PAGE_SIZE = 250
IMAGE_WIDTH = 640          # photo box renders at <=480 CSS px; 640 covers retina
REQUEST_TIMEOUT = 90
MAX_RETRIES = 5
COMMONS_DELAY = 0.4        # be polite; raise if Commons starts 429ing

# Non-commercial / non-free licence markers, matched case-insensitively against
# Commons' LicenseShortName. Entries matching these are flagged, never dropped.
NONCOMMERCIAL_PATTERNS = [
    r"\bnc\b", r"non-?commercial", r"\bfair use\b", r"non-?free", r"\bcc[- ]by[- ]nc",
]

# ---------------------------------------------------------------------------
# SPARQL
# ---------------------------------------------------------------------------

# The inner subquery is the pagination spine; everything else is optional so a
# missing coordinate or image never drops the row.
QUERY_TEMPLATE = """
SELECT ?item ?labelEn ?labelFr ?labelEs ?descEn ?descFr ?descEs
       ?altEn ?altFr ?altEs ?lat ?lon ?image ?sitelinks ?inscribed
       ?country ?countryEn ?countryFr ?countryEs
       ?continent ?continentEn ?criterionEn WHERE {
  {
    SELECT ?item WHERE { ?item p:P1435/ps:P1435 wd:%(qid)s . }
    ORDER BY ?item
    LIMIT %(limit)d
    OFFSET %(offset)d
  }
  OPTIONAL { ?item rdfs:label ?labelEn . FILTER(lang(?labelEn)="en") }
  OPTIONAL { ?item rdfs:label ?labelFr . FILTER(lang(?labelFr)="fr") }
  OPTIONAL { ?item rdfs:label ?labelEs . FILTER(lang(?labelEs)="es") }
  OPTIONAL { ?item schema:description ?descEn . FILTER(lang(?descEn)="en") }
  OPTIONAL { ?item schema:description ?descFr . FILTER(lang(?descFr)="fr") }
  OPTIONAL { ?item schema:description ?descEs . FILTER(lang(?descEs)="es") }
  OPTIONAL { ?item skos:altLabel ?altEn . FILTER(lang(?altEn)="en") }
  OPTIONAL { ?item skos:altLabel ?altFr . FILTER(lang(?altFr)="fr") }
  OPTIONAL { ?item skos:altLabel ?altEs . FILTER(lang(?altEs)="es") }
  OPTIONAL { ?item wdt:P18 ?image . }
  OPTIONAL { ?item wikibase:sitelinks ?sitelinks . }
  OPTIONAL { ?item p:P625/psv:P625 [ wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon ] . }
  OPTIONAL { ?item p:P1435 [ ps:P1435 wd:%(qid)s ; pq:P580 ?inscribed ] . }
  OPTIONAL {
    ?item wdt:P17 ?country .
    OPTIONAL { ?country rdfs:label ?countryEn . FILTER(lang(?countryEn)="en") }
    OPTIONAL { ?country rdfs:label ?countryFr . FILTER(lang(?countryFr)="fr") }
    OPTIONAL { ?country rdfs:label ?countryEs . FILTER(lang(?countryEs)="es") }
  }
  OPTIONAL {
    ?item wdt:P30 ?continent .
    OPTIONAL { ?continent rdfs:label ?continentEn . FILTER(lang(?continentEn)="en") }
  }
  OPTIONAL {
    ?item wdt:P2614 ?criterion .
    OPTIONAL { ?criterion rdfs:label ?criterionEn . FILTER(lang(?criterionEn)="en") }
  }
}
"""

# Coordinate fallback for entries with no P625 of their own -- overwhelmingly
# intangible elements, which are practices rather than places. One batched
# query rather than one request per country.
COUNTRY_COORD_QUERY = """
SELECT ?country ?lat ?lon WHERE {
  VALUES ?country { %s }
  ?country p:P625/psv:P625 [ wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon ] .
}
"""

# Continent fallback: derive from the country when the item has no P30.
COUNTRY_CONTINENT_QUERY = """
SELECT ?country ?continentEn WHERE {
  VALUES ?country { %s }
  ?country wdt:P30 ?continent .
  ?continent rdfs:label ?continentEn . FILTER(lang(?continentEn)="en")
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
        if "VALUES ?country" in query and "P625" in query:
            return self._load("country_coords.json") or {"results": {"bindings": []}}
        if "VALUES ?country" in query:
            return self._load("country_continents.json") or {"results": {"bindings": []}}
        qid = re.search(r"wd:(Q\d+)", query)
        offset = int(re.search(r"OFFSET (\d+)", query).group(1))
        if offset > 0:  # fixtures are a single page
            return {"results": {"bindings": []}}
        return self._load(f"{qid.group(1)}.json") or {"results": {"bindings": []}}

    def imageinfo(self, filename):
        data = self._load("commons.json") or {}
        return data.get(filename)


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


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


# ---------------------------------------------------------------------------
# Field derivation
# ---------------------------------------------------------------------------

CONTINENT_CODES = {
    "europe": "EU", "asia": "AS", "africa": "AF", "north america": "NA",
    "south america": "SA", "oceania": "OC", "australia": "OC",
    "antarctica": "AN", "insular oceania": "OC", "americas": "NA",
}

CULTURAL_CRITERIA = {"i", "ii", "iii", "iv", "v", "vi"}
NATURAL_CRITERIA = {"vii", "viii", "ix", "x"}


def continent_code(label):
    if not label:
        return None
    return CONTINENT_CODES.get(label.strip().lower())


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
    has_c = bool(found & CULTURAL_CRITERIA)
    has_n = bool(found & NATURAL_CRITERIA)
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
# Fetch + group
# ---------------------------------------------------------------------------

def fetch_designation(qid, kind, limit=None):
    """Page through one designation, merging the OPTIONAL cross product per item."""
    items = {}
    offset = 0
    page_size = min(PAGE_SIZE, limit) if limit else PAGE_SIZE

    while True:
        rows = sparql(QUERY_TEMPLATE % {"qid": qid, "limit": page_size, "offset": offset})
        if not rows:
            break

        seen_this_page = set()
        for row in rows:
            q = qid_of(val(row, "item"))
            if not q:
                continue
            seen_this_page.add(q)
            it = items.setdefault(q, {
                "qid": q, "type": kind, "aliases": {"en": set(), "fr": set(), "es": set()},
                "criteria": set(), "images": [],
            })
            for lang in ("En", "Fr", "Es"):
                key = lang.lower()
                if val(row, f"label{lang}"):
                    it.setdefault("names", {})[key] = val(row, f"label{lang}")
                if val(row, f"desc{lang}"):
                    it.setdefault("desc", {})[key] = val(row, f"desc{lang}")
                if val(row, f"alt{lang}"):
                    it["aliases"][key].add(val(row, f"alt{lang}"))
            for key, src in (("lat", "lat"), ("lng", "lon"), ("sitelinks", "sitelinks"),
                             ("inscribed", "inscribed")):
                if val(row, src) and key not in it:
                    it[key] = val(row, src)
            if val(row, "country") and "country_qid" not in it:
                it["country_qid"] = qid_of(val(row, "country"))
                it["country"] = {
                    "en": val(row, "countryEn"),
                    "fr": val(row, "countryFr"),
                    "es": val(row, "countryEs"),
                }
            if val(row, "continentEn") and "continent_label" not in it:
                it["continent_label"] = val(row, "continentEn")
            if val(row, "criterionEn"):
                it["criteria"].add(val(row, "criterionEn"))
            img = val(row, "image")
            if img and img not in it["images"]:
                it["images"].append(img)

        offset += page_size
        print(f"  {kind}: {len(items)} items after offset {offset}")
        if limit and len(items) >= limit:
            break
        if len(seen_this_page) < page_size / 4:
            # Last page: the cross product means row count is a poor end signal,
            # but a page yielding very few distinct items means we ran out.
            if len(rows) < page_size:
                break
    return items


def resolve_country_fallbacks(items):
    """Task 2: fill lat/lng from the linked country, and continent likewise."""
    need_coord = {it["country_qid"] for it in items.values()
                  if "lat" not in it and it.get("country_qid")}
    need_continent = {it["country_qid"] for it in items.values()
                      if not it.get("continent_label") and it.get("country_qid")}

    coords, continents = {}, {}
    if need_coord:
        values = " ".join(f"wd:{q}" for q in sorted(need_coord))
        for row in sparql(COUNTRY_COORD_QUERY % values):
            coords[qid_of(val(row, "country"))] = (val(row, "lat"), val(row, "lon"))
        print(f"  country-coordinate fallback: resolved {len(coords)}/{len(need_coord)} countries")
    if need_continent:
        values = " ".join(f"wd:{q}" for q in sorted(need_continent))
        for row in sparql(COUNTRY_CONTINENT_QUERY % values):
            continents.setdefault(qid_of(val(row, "country")), val(row, "continentEn"))
        print(f"  continent fallback: resolved {len(continents)}/{len(need_continent)} countries")

    filled = 0
    for it in items.values():
        cq = it.get("country_qid")
        if "lat" not in it and cq in coords:
            it["lat"], it["lng"] = coords[cq]
            it["approx_location"] = True
            filled += 1
        if not it.get("continent_label") and cq in continents:
            it["continent_label"] = continents[cq]
    print(f"  filled {filled} missing coordinates from country centroids")


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
    along with the file -- task 5 depends on this never being dropped."""
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
        credit = info["artist"] or info["credit"] or "Wikimedia Commons"
        entry["image"] = {
            "path": path,
            "file": filename,
            "license": info["license"],
            "credit": credit,
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
    """Task 3: tier 1 = widely recognized ... 3 = hidden gem.

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
        print(f"  {kind}: n={n} min={vals[0]} median={percentile(vals, 50):.0f} max={vals[-1]}")
        print(f"    tier1 >= {b1} sitelinks | tier2 >= {b2} | tier3 below that")
        counts = Counter(e["tier"] for e in group)
        print(f"    tier1={counts[1]} tier2={counts[2]} tier3={counts[3]}")
        if b1 == b2:
            print(f"    NOTE: {kind} tier boundaries fall on the same sitelink count "
                  f"({b1}); the tier-2/3 split there is arbitrary.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def to_entry(it, seen_ids, coverage):
    names = it.get("names", {})
    name_en = names.get("en") or names.get("fr") or names.get("es")
    if not name_en or "lat" not in it:
        return None  # unusable: no name at all, or no coordinate even after fallback

    slug = slugify(name_en, it["qid"])
    if slug in seen_ids:
        slug = f"{slug}-{it['qid'].lower()}"
    seen_ids.add(slug)

    desc = it.get("desc", {})
    entry = {
        "id": slug,
        "qid": it["qid"],
        "type": it["type"],
        "names": {},
        "lat": round(float(it["lat"]), 4),
        "lng": round(float(it["lng"]), 4),
        "sitelinks": int(it.get("sitelinks") or 0),
        "continent": continent_code(it.get("continent_label")),
        "category": category_code(it["type"], it["criteria"]),
    }

    for lang in ("en", "fr", "es"):
        entry["names"][lang] = names.get(lang) or name_en
        coverage["names"][lang] += 1 if names.get(lang) else 0
        if desc.get(lang):
            entry.setdefault("desc", {})[lang] = desc[lang]
            coverage["desc"][lang] += 1
    if "desc" in entry:  # fall back to English so no language shows an empty clue
        fallback = entry["desc"].get("en") or next(iter(entry["desc"].values()))
        for lang in ("en", "fr", "es"):
            entry["desc"].setdefault(lang, fallback)

    if it.get("country_qid"):
        entry["country"] = it["country_qid"]
    if it.get("inscribed"):
        m = re.match(r"(-?\d{1,4})-", it["inscribed"])
        if m:
            entry["year"] = int(m.group(1))
    if it.get("approx_location"):
        entry["approx"] = True

    aliases = set()
    for lang in ("en", "fr", "es"):
        aliases |= set(list(it["aliases"][lang])[:4])
    aliases |= {v for v in entry["names"].values()}
    entry["aliases"] = sorted({a for a in aliases if a})[:10]
    return entry


def build_country_table(items):
    """One shared en/fr/es lookup instead of repeating country names ~2,000 times."""
    table = {}
    for it in items.values():
        cq = it.get("country_qid")
        if not cq or cq in table or not it.get("country"):
            continue
        c = it["country"]
        en = c.get("en") or c.get("fr") or c.get("es")
        if not en:
            continue
        table[cq] = {"en": en, "fr": c.get("fr") or en, "es": c.get("es") or en}
    return table


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

    all_items = {}
    for qid, kind, expected in DESIGNATIONS:
        print(f"\nFetching {kind} ({qid})...")
        items = fetch_designation(qid, kind, limit=args.limit)
        print(f"  {kind}: {len(items)} distinct items")
        if not args.limit and not args.fixture and len(items) < expected * 0.5:
            print(f"  WARNING: expected ~{expected}, got {len(items)}. "
                  f"Check that {qid} is the right P1435 value.", file=sys.stderr)
        all_items.update(items)

    print("\nResolving country fallbacks...")
    resolve_country_fallbacks(all_items)

    coverage = {"names": Counter(), "desc": Counter()}
    seen_ids, dataset = set(), []
    for it in sorted(all_items.values(), key=lambda i: i["qid"]):
        entry = to_entry(it, seen_ids, coverage)
        if entry:
            dataset.append(entry)
    dropped = len(all_items) - len(dataset)
    print(f"\n{len(dataset)} usable entries ({dropped} dropped: no name or no coordinate)")

    print("\nFame tiers (rank tertiles within each type):")
    assign_tiers(dataset)

    if not args.skip_images:
        print("\nDownloading photos...")
        for i, entry in enumerate(dataset, 1):
            it = all_items[entry["qid"]]
            if it["images"]:
                download_image(entry, it["images"])
            if i % 50 == 0:
                got = sum(1 for e in dataset[:i] if "image" in e)
                print(f"  {i}/{len(dataset)} processed, {got} photos")

    dataset.sort(key=lambda e: -e["sitelinks"])  # fame-ranked, highest first

    n = len(dataset) or 1
    meta = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(dataset),
        "countsByType": dict(Counter(e["type"] for e in dataset)),
        "withPhoto": sum(1 for e in dataset if "image" in e),
        "approxCoordinates": sum(1 for e in dataset if e.get("approx")),
        "nonCommercialPhotos": sum(1 for e in dataset if e.get("image", {}).get("nonCommercial")),
        "translationCoverage": {
            field: {lang: round(100 * coverage[field][lang] / n, 1) for lang in ("en", "fr", "es")}
            for field in ("names", "desc")
        },
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "countries": build_country_table(all_items),
                   "entries": dataset}, f, ensure_ascii=False, separators=(",", ":"))

    nc = [{"id": e["id"], "name": e["names"]["en"], "license": e["image"]["license"],
           "file": e["image"]["file"]}
          for e in dataset if e.get("image", {}).get("nonCommercial")]
    nc_path = os.path.join(os.path.dirname(args.out) or ".", "noncommercial_images.json")
    with open(nc_path, "w", encoding="utf-8") as f:
        json.dump(nc, f, ensure_ascii=False, indent=1)

    print(f"\nWrote {len(dataset)} entries to {args.out}")
    print(f"  photos: {meta['withPhoto']}  |  country-centroid coords: {meta['approxCoordinates']}")
    cov = meta["translationCoverage"]
    print(f"  name coverage:  en {cov['names']['en']}%  fr {cov['names']['fr']}%  es {cov['names']['es']}%")
    print(f"  descr coverage: en {cov['desc']['en']}%  fr {cov['desc']['fr']}%  es {cov['desc']['es']}%")
    if cov["names"]["fr"] < 60 or cov["names"]["es"] < 60:
        print("  WARNING: most entries fall back to the English name in fr/es. "
              "Worth flagging before shipping as 'trilingual'.", file=sys.stderr)
    if nc:
        print(f"  {len(nc)} photos are non-commercial-only -- listed in {nc_path} (kept, not dropped)")


if __name__ == "__main__":
    main()
