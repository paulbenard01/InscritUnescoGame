"""
Heritle dataset + image builder.

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
emitted as stable codes ("EU", "cultural", ...) and translated in heritle.html's
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
# start rejecting requests. Override with HERITLE_CONTACT once the repo is public.
CONTACT = os.environ.get("HERITLE_CONTACT", "paulbenard01@gmail.com")
HEADERS = {"User-Agent": f"Heritle/1.0 ({CONTACT}; personal heritage guessing game)"}

SPARQL_URL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# The two pools the game draws from. They are selected by different
# properties, which is the whole story of how this went wrong once already.
#
# World Heritage Sites carry a heritage designation, P1435 = Q9259.
#
# Intangible elements do not carry a heritage designation at all. The value
# originally briefed for them, Q1459900, is the World Heritage *tentative*
# list -- candidate places, not inscribed traditions -- which is why the game
# once served Roman ruins and national parks as intangible heritage, Sbeitla
# among them. tools/find_ich.py established the real modelling: a status,
# P3259, whose values are the individual lists (counts from 2026-09-11):
#
#   Q110319947  Representative List                       823 items
#   Q17323370   In Need of Urgent Safeguarding             94 items
#   Q877988     Masterpieces of the Oral and Intangible    89 items
#
# Only the first two are selected. Masterpieces predates the Representative
# List and was folded into it, so its elements are already counted there;
# adding it would double-count. 823 + 94 = 917 against about 849 officially
# inscribed -- Wikidata is a little looser than the register, as it is for
# World Heritage too.
#
# Two traps worth keeping written down. Q110319947 also exists as a *P1435*
# value, where it holds three items: selecting on the wrong property yields
# almost nothing while looking like it worked. And unlike P757 for World
# Heritage, there is no official identifier property for intangible elements,
# so list membership is the gate -- see filter_to_official.
POOLS = [
    {
        "kind": "material",
        "expected": 1273,
        "spine": "?item p:P1435/ps:P1435 wd:Q9259 .",
        "inscribed": "?item p:P1435 [ ps:P1435 wd:Q9259 ; pq:P580 ?inscribed_ ] .",
        "label": "P1435 = Q9259",
    },
    {
        "kind": "immaterial",
        "expected": 917,
        "spine": ("VALUES ?ichList_ { wd:Q110319947 wd:Q17323370 } "
                  "?item p:P3259/ps:P3259 ?ichList_ ."),
        # Any P3259 statement's start date: an element inscribed on one list
        # and later moved to another carries both, and either date is the right
        # sort of answer for "when was this recognised".
        "inscribed": "?item p:P3259 [ pq:P580 ?inscribed_ ] .",
        "label": "P3259 = Q110319947 / Q17323370",
    },
]

PAGE_SIZE = 250
ALIASES_PER_LANG = 4
# The photo box crops to 16:10 at ~450 CSS px, but tapping it opens the full
# frame, so the source needs to carry real detail -- architecture, vegetation,
# signage are what a player reads a continent off.
# Measured against live Commons: 1024px thumbnails average ~408 KB, and even
# re-encoded at quality 80 they only fall to ~291 KB. Across ~1,900 photos that
# is ~553 MB -- too much to carry in git, and close to the 1 GB GitHub Pages
# ceiling. So by default the dataset stores Commons' own CDN URL and the game
# loads from there, which costs nothing in the repo and lets the width go
# *higher* than a self-hosted copy could afford.
#
# --download-images still fetches local copies (they take precedence in the
# game), for offline play or if hotlinking ever needs to stop. Budget roughly
# IMAGE_WIDTH^2 -- 640px lands near 150 MB, 1024px near 553 MB.
IMAGE_WIDTH = 1280
IMAGE_QUALITY = 80
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
       (GROUP_CONCAT(DISTINCT ?inscribedBy_;  separator="%(sep)s") AS ?inscribedBy)
       (GROUP_CONCAT(DISTINCT ?origin_;       separator="%(sep)s") AS ?origin)
       (GROUP_CONCAT(DISTINCT ?jurisdiction_; separator="%(sep)s") AS ?jurisdiction)
       (GROUP_CONCAT(DISTINCT ?indigenous_;   separator="%(sep)s") AS ?indigenous)
       (GROUP_CONCAT(DISTINCT ?continentEn_; separator="%(sep)s") AS ?continentEn)
       (GROUP_CONCAT(DISTINCT ?image_;       separator="%(sep)s") AS ?image)
       (SAMPLE(?commonsCat_) AS ?commonsCat)
       (GROUP_CONCAT(DISTINCT ?criterionEn_; separator="%(sep)s") AS ?criterionEn)
       (GROUP_CONCAT(DISTINCT ?siteId_;      separator="%(sep)s") AS ?siteId)
       (GROUP_CONCAT(DISTINCT ?officialUrl_; separator="%(sep)s") AS ?officialUrl)
WHERE {
  {
    SELECT DISTINCT ?item WHERE { %(spine)s }
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
  # P18 is the main photo, but a site often has only one and it can be a poor
  # one -- a signpost, a detail, a museum case. These are the other image
  # properties Wikidata uses, and the Commons category is the deep well.
  OPTIONAL { ?item wdt:P18 ?image_ . }
  OPTIONAL { ?item wdt:P3451 ?image_ . }    # nighttime view
  OPTIONAL { ?item wdt:P5252 ?image_ . }    # winter view
  OPTIONAL { ?item wdt:P8592 ?image_ . }    # aerial view
  OPTIONAL { ?item wdt:P2716 ?image_ . }    # collage
  OPTIONAL { ?item wdt:P373 ?commonsCat_ . }
  OPTIONAL { ?item wikibase:sitelinks ?sitelinks_ . }
  OPTIONAL { ?item wdt:P17 ?country_ . }
  # Half the intangible elements carry no P17 at all: a tradition is not
  # obviously a thing that "has a country", and Wikidata records the same fact
  # under several other names. Dropping them cost 407 of 916 elements -- most
  # of the register -- so the fallbacks are fetched and ranked in Python.
  OPTIONAL { ?item p:P3259 [ pq:P17 ?inscribedBy_ ] . }
  OPTIONAL { ?item wdt:P495 ?origin_ . }
  OPTIONAL { ?item wdt:P1001 ?jurisdiction_ . }
  OPTIONAL { ?item wdt:P2341 ?indigenous_ . }
  # P757 is the official World Heritage site number -- the exact join key to
  # the published list. Intangible elements have no equivalent property, so any
  # statement pointing at an official element page stands in for one.
  OPTIONAL { ?item wdt:P757 ?siteId_ . }
  OPTIONAL {
    ?item ?anyProp_ ?officialUrl_ .
    FILTER(isIRI(?officialUrl_) && CONTAINS(STR(?officialUrl_), "ich.unesco.org"))
  }
  OPTIONAL { %(inscribed)s }
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

# The published World Heritage list, used to keep only genuine inscriptions.
OFFICIAL_WHS_URL = "https://whc.unesco.org/en/list/xml/"


def official_inscriptions():
    """Parent site numbers on the published World Heritage list.

    Replayed from tests/fixtures/official_ids.json under --fixture, so the
    filter is exercised offline: a KeyError here previously surfaced only
    after twenty minutes of live crawling.

    Only identifiers are read -- no titles or descriptions are copied into the
    dataset, so nothing of theirs is redistributed. Wikidata's designation
    property is applied far more loosely than the published list (1,946 items
    against 1,273 inscriptions), sweeping in components of serial sites and
    buildings that were never inscribed, so without this gate the game labels
    things as World Heritage Sites that are not.
    """
    import xml.etree.ElementTree as ET
    if FIXTURES:
        ids = FIXTURES._load("official_ids.json") or []
        print(f"  official list (fixture): {len(ids)} inscriptions")
        return set(ids) or None
    try:
        r = requests.get(OFFICIAL_WHS_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"  WARNING: could not read the official list ({exc}); "
              f"keeping every designated item.", file=sys.stderr)
        return None
    ids = set()
    for row in root.iter():
        if row.tag.lower() not in ("row", "site"):
            continue
        for child in row:
            if child.tag.lower() in ("id_number", "id") and (child.text or "").strip():
                stem = re.match(r"(\d+)", child.text.strip())
                if stem:
                    ids.add(stem.group(1))
                break
    print(f"  official list: {len(ids)} inscriptions")
    return ids or None


def parent_site_id(sid):
    """Wikidata's P757 carries suffixes the published list doesn't -- '1bis',
    '1153rev', '813-001', '1558-2023'. All name the same inscription as their
    numeric stem."""
    m = re.match(r"(\d+)", str(sid).strip())
    return m.group(1) if m else None


def filter_to_official(items):
    """Drop anything not on the published list, and collapse serial-site
    components onto the inscription they belong to."""
    official = official_inscriptions()
    if not official:
        return items

    kept, no_id, unlisted = {}, 0, 0
    by_inscription = {}
    for qid, it in items.items():
        if it["type"] != "material":
            kept[qid] = it
            continue
        stems = {parent_site_id(s) for s in it.get("site_ids", [])} - {None}
        if not stems:
            no_id += 1
            continue
        listed = stems & official
        if not listed:
            unlisted += 1
            continue
        stem = sorted(listed)[0]
        it["site_id"] = stem
        # One entry per inscription: prefer the item that *is* the inscription
        # (an unsuffixed id), then the better-known one.
        exact = any(str(x).strip() == stem for x in it.get("site_ids", []))
        rank = (1 if exact else 0, int(it.get("sitelinks") or 0))
        prev = by_inscription.get(stem)
        if prev is None or rank > prev[0]:
            by_inscription[stem] = (rank, qid, it)

    for _rank, qid, it in by_inscription.values():
        kept[qid] = it
    material_in = sum(1 for i in items.values() if i["type"] == "material")
    merged = material_in - no_id - unlisted - len(by_inscription)
    print(f"  material: {material_in} -> {len(by_inscription)} inscriptions "
          f"({no_id} with no site number, {unlisted} not on the list, "
          f"{merged} merged as components/duplicates)")

    # Intangible elements have no equivalent identifier property on Wikidata.
    # Report how many at least link to an official element page, but don't gate
    # on it: a partly-populated property would silently delete real elements.
    imm = [i for i in items.values() if i["type"] == "immaterial"]
    linked = sum(1 for i in imm if i.get("official_urls"))
    print(f"  intangible: {len(imm)} kept, {linked} of which link to an official "
          f"element page (no identifier property to filter on)")
    return kept


# Everything country-shaped in one batched query: names for the lookup table,
# a coordinate for entries that have none of their own (intangible elements are
# practices rather than places, so most lack P625), and a continent fallback.
COUNTRY_INFO_QUERY = """
SELECT ?country ?cEn ?cFr ?cEs ?coord ?contEn ?iso WHERE {
  VALUES ?country { %s }
  # ISO 3166-1 alpha-2. The map's country polygons are keyed by it, so this is
  # what lets a tap on the map resolve to one of these countries.
  OPTIONAL { ?country wdt:P297 ?iso . }
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
        if int(re.search(r"OFFSET (\d+)", query).group(1)) > 0:
            return {"results": {"bindings": []}}  # fixtures are a single page
        # Matched on the pool's own spine, so the mapping cannot drift from the
        # queries it is meant to identify. Keying on a bare property name was
        # not enough: both pools now mention P3259 -- the intangible one selects
        # on it, the material one reads a country qualifier off it -- so the
        # material query was being answered with the intangible fixture.
        for pool in POOLS:
            if pool["spine"] in query:
                return self._load(f"{pool['kind']}.json") or {"results": {"bindings": []}}
        return {"results": {"bindings": []}}

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


# Where an item's countries come from, best source first. These are ranked
# rather than merged: P17 is "this is in/of that country", while country of
# origin, jurisdiction and indigenous-to answer slightly different questions.
# Merging them would let a tradition's historical origin join the set of
# countries that actually inscribed it, which is not the same claim.
COUNTRY_SOURCES = [
    ("inscribed-by", "inscribedBy"),   # a country qualifier on the inscription
    ("P17", "country"),
    ("P495", "origin"),
    ("P1001", "jurisdiction"),
    ("P2341", "indigenous"),
]


def country_sources(row):
    """(source name, qids) from the best-populated source this row has."""
    for name, field in COUNTRY_SOURCES:
        qids = [q for q in (qid_of(x) for x in multi(row, field)) if q]
        if qids:
            return name, qids
    return None, []


def fetch_designation(pool, limit=None):
    """Page through one pool. The query returns one row per item."""
    kind = pool["kind"]
    items = {}
    offset = 0
    page_size = min(PAGE_SIZE, limit) if limit else PAGE_SIZE

    while True:
        rows = sparql(QUERY_TEMPLATE % {"spine": pool["spine"],
                                        "inscribed": pool["inscribed"],
                                        "limit": page_size,
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
                # Every country, not just the first. Intangible elements are
                # routinely inscribed by many states at once -- falconry by two
                # dozen -- and keeping only whichever one Wikidata happened to
                # list first made the answer arbitrary: Nowruz came out as
                # "Kurdistan", Diwali as "Mauritius". The first is still the
                # primary for display and for the coordinate fallback.
                "country_qids": country_sources(row)[1],
                "country_source": country_sources(row)[0],
                "continent_labels": multi(row, "continentEn"),
                "criteria": multi(row, "criterionEn"),
                "site_ids": multi(row, "siteId"),
                "official_urls": multi(row, "officialUrl"),
                "images": multi(row, "image"),
                "commons_cat": val(row, "commonsCat"),
                "aliases": {"en": [], "fr": [], "es": []},
            }
            it["country_qid"] = it["country_qids"][0] if it["country_qids"] else None
            coord = parse_coord(val(row, "coord"))
            if coord:
                it["lat"], it["lng"] = coord
            page[q] = it

        aliases = fetch_aliases(list(page))
        for q, it in page.items():
            for lang, vals in aliases.get(q, {}).items():
                it["aliases"][lang] = vals

        before = len(items)
        items.update(page)
        offset += page_size
        print(f"  {kind}: {len(items)} items after offset {offset}")
        if limit and len(items) >= limit:
            break
        # Stop only on an empty page, never on a short one. An item with two
        # P1435 statements used to appear twice in the (non-DISTINCT) spine, so
        # a full page of 250 rows yielded ~246 distinct items -- and a
        # "short page means the end" test then ended pagination on page one,
        # silently capping the dataset at a fifth of its real size.
        if len(items) == before:
            break
    return items


def resolve_countries(items):
    """Batch-resolve every referenced country: names, centroid, continent."""
    qids = sorted({q for it in items.values() for q in it.get("country_qids", [])})
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
            if val(row, "iso") and "iso" not in rec:
                rec["iso"] = val(row, "iso").upper()
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


try:
    from PIL import Image
except ImportError:
    Image = None
_warned_no_pillow = False


def recompress(data):
    """Re-encode a Commons thumbnail at IMAGE_QUALITY, keeping its dimensions.

    Returns the original bytes unchanged if Pillow is missing or the image
    won't decode -- a photo at the wrong size beats no photo.
    """
    global _warned_no_pillow
    if Image is None:
        if not _warned_no_pillow:
            print("  note: Pillow not installed, keeping Commons' original file sizes "
                  "(~3x larger). pip install Pillow", file=sys.stderr)
            _warned_no_pillow = True
        return data
    try:
        import io
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")  # drops alpha and palette modes JPEG can't hold
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=IMAGE_QUALITY, optimize=True, progressive=True)
        out = buf.getvalue()
        return out if len(out) < len(data) else data
    except Exception:
        return data


def commons_imageinfo_many(filenames):
    """imageinfo for up to 50 files in one request.

    One request per file meant roughly 1,800 round trips at the polite delay --
    about a quarter of an hour of the crawl spent on handshakes. Batching pays
    for fetching several photos per entry instead of one.
    """
    out = {}
    if not filenames:
        return out
    if FIXTURES:
        for f in filenames:
            info = FIXTURES.imageinfo(f)
            if info:
                out[f] = info
        return out
    for i in range(0, len(filenames), 50):
        batch = filenames[i:i + 50]
        titles = "|".join(f"File:{f}" for f in batch)
        for attempt in range(3):
            try:
                r = requests.get(COMMONS_API, params={
                    "action": "query", "titles": titles, "prop": "imageinfo",
                    "iiprop": "url|extmetadata", "iiurlwidth": IMAGE_WIDTH,
                    "format": "json",
                }, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After") or 5))
                    continue
                r.raise_for_status()
                pages = r.json().get("query", {}).get("pages", {})
                for page in pages.values():
                    info = (page.get("imageinfo") or [None])[0]
                    if not info:
                        continue
                    meta = info.get("extmetadata", {})
                    strip = lambda x: re.sub(r"<[^<]+?>", "", x or "").strip()
                    name = page.get("title", "").split(":", 1)[-1]
                    out[name] = {
                        "thumb_url": info.get("thumburl") or info.get("url"),
                        "license": meta.get("LicenseShortName", {}).get("value", "unknown"),
                        "artist": strip(meta.get("Artist", {}).get("value", "")),
                        "credit": strip(meta.get("Credit", {}).get("value", "")),
                        "descriptionurl": info.get("descriptionurl", ""),
                    }
                break
            except (requests.RequestException, ValueError, KeyError):
                time.sleep(2 ** (attempt + 1))
        time.sleep(COMMONS_DELAY)
    return out


def commons_category_files(category, limit=6):
    """Photo filenames from a Commons category.

    Wikidata often knows one image for a site and it is not always a useful
    one -- a plaque, a detail, a museum case. The category is where the
    photographs actually are.
    """
    if FIXTURES or not category:
        return []
    try:
        r = requests.get(COMMONS_API, params={
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{category}", "cmtype": "file",
            "cmlimit": limit, "format": "json",
        }, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        members = r.json().get("query", {}).get("categorymembers", [])
        return [m["title"].split(":", 1)[-1] for m in members]
    except (requests.RequestException, ValueError, KeyError):
        return []
    finally:
        time.sleep(COMMONS_DELAY)


PHOTOS_PER_ENTRY = 4

# Commons categories hold more than photographs: pronunciation recordings,
# videos, scanned PDFs, and -- worst for this game -- SVG locator maps, which
# would show the player exactly which country the answer is in. Only raster
# photographs get through.
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif")


def is_photo(filename):
    return filename.lower().endswith(PHOTO_EXT)


def resolve_photos(dataset, all_items, download=False):
    """Attach up to PHOTOS_PER_ENTRY usable Commons photos to every entry.

    One photo was a coin toss: plenty of inscriptions have a single P18 that
    says almost nothing about the place -- a plaque, a doorway, a museum case.
    The game reveals another with each wrong guess, so a weak first photo is a
    slow start rather than a dead round.

    Filenames from every image property are gathered first and looked up in
    batches, because the licence and attribution have to ride along with each
    file and doing that one request at a time dominated the crawl.
    """
    wanted = {}
    for entry in dataset:
        it = all_items[entry["qid"]]
        names = []
        for uri in it.get("images", []):
            name = unquote(uri.split("/")[-1]).replace("_", " ")
            if is_photo(name):
                names.append(name)
        entry["_photo_names"] = names[:PHOTOS_PER_ENTRY * 2]
        for n in entry["_photo_names"]:
            wanted[n] = None

    # Anything still thin goes to the Commons category, which is where the
    # photographs of a place actually live.
    thin = [e for e in dataset if len(e["_photo_names"]) < PHOTOS_PER_ENTRY]
    print(f"  {len(dataset) - len(thin)} entries already have "
          f"{PHOTOS_PER_ENTRY}+ candidates; {len(thin)} need the category")
    for i, entry in enumerate(thin, 1):
        cat = all_items[entry["qid"]].get("commons_cat")
        if not cat:
            continue
        extra = commons_category_files(cat, limit=PHOTOS_PER_ENTRY * 2)
        for n in extra:
            if is_photo(n) and n not in entry["_photo_names"]:
                entry["_photo_names"].append(n)
                wanted[n] = None
        if i % 100 == 0:
            print(f"  category lookups {i}/{len(thin)}")

    print(f"  looking up {len(wanted)} distinct files on Commons")
    info_by_name = commons_imageinfo_many(sorted(wanted))

    for entry in dataset:
        photos = []
        for name in entry.pop("_photo_names", []):
            info = info_by_name.get(name)
            if not info or not info.get("thumb_url"):
                continue
            photo = {
                "url": info["thumb_url"],
                "file": name,
                "license": info["license"],
                "credit": info["artist"] or info["credit"] or "Wikimedia Commons",
                "source": info["descriptionurl"],
            }
            if is_noncommercial(info["license"]):
                photo["nonCommercial"] = True
            photos.append(photo)
            if len(photos) >= PHOTOS_PER_ENTRY:
                break
        if photos:
            # `image` stays the first photo so nothing downstream has to know
            # about the list; `photos` is what the reveal-on-miss walks.
            entry["image"] = photos[0]
            if len(photos) > 1:
                entry["photos"] = photos
    stray = sum(1 for e in dataset
                for ph in (e.get("photos") or ([e["image"]] if "image" in e else []))
                if not is_photo(ph["file"]))
    if stray:
        print(f"  WARNING: {stray} non-photograph file(s) got through", file=sys.stderr)
    got = sum(1 for e in dataset if "image" in e)
    extra = sum(len(e.get("photos", [])) for e in dataset)
    print(f"  {got}/{len(dataset)} entries have a photo; "
          f"{sum(1 for e in dataset if e.get('photos'))} have more than one "
          f"({extra} photos in all)")


def download_image(entry, image_uris, out_dir="images", download=False):
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
        if FIXTURES or not download:
            pass  # metadata only: the game loads info["thumb_url"] from Commons
        elif not os.path.exists(path):  # resumable: don't re-download on a rerun
            os.makedirs(out_dir, exist_ok=True)
            try:
                img = requests.get(info["thumb_url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
                img.raise_for_status()
                if not img.headers.get("Content-Type", "").startswith("image/"):
                    continue
                with open(path, "wb") as f:
                    f.write(recompress(img.content))
            except requests.RequestException:
                continue
        entry["image"] = {
            "url": info["thumb_url"],
            "file": filename,
            "license": info["license"],
            "credit": info["artist"] or info["credit"] or "Wikimedia Commons",
            "source": info["descriptionurl"],
        }
        if download:
            entry["image"]["path"] = path
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
    # Only when there really are several: an extra single-element list on every
    # one of 1,263 sites is dead weight in a file the game downloads.
    if len(it.get("country_qids") or []) > 1:
        entry["countries"] = it["country_qids"]
    if it.get("inscribed"):
        m = re.match(r"(-?\d{1,4})-", it["inscribed"])
        if m:
            entry["year"] = int(m.group(1))
    if it.get("approx_location"):
        entry["approx"] = True
    if it.get("site_id"):
        entry["siteId"] = it["site_id"]

    aliases = {a for vals in it["aliases"].values() for a in vals}
    aliases |= set(entry["names"].values())
    entry["aliases"] = sorted({a for a in aliases if a})[:10]
    return entry


def main():
    global FIXTURES
    ap = argparse.ArgumentParser(description="Build the Heritle dataset.")
    ap.add_argument("--limit", type=int, help="cap items per designation (smoke test)")
    ap.add_argument("--skip-images", action="store_true",
                    help="skip Commons entirely: no photo URLs, no downloads")
    ap.add_argument("--download-images", action="store_true",
                    help="also save local copies under images/ (adds ~550 MB at "
                         "the default width; the game prefers them when present)")
    ap.add_argument("--fixture", help="replay recorded responses from this dir (offline)")
    ap.add_argument("--keep-unofficial", action="store_true",
                    help="skip the official-list filter and keep every designated item")
    ap.add_argument("--out", default="data/dataset.json")
    args = ap.parse_args()

    if args.fixture:
        FIXTURES = Fixtures(args.fixture)
        print(f"OFFLINE: replaying fixtures from {args.fixture}")

    all_items, dual = {}, []
    for pool in POOLS:
        kind, expected = pool["kind"], pool["expected"]
        print(f"\nFetching {kind} ({pool['label']})...")
        items = fetch_designation(pool, limit=args.limit)
        print(f"  {kind}: {len(items)} distinct items")
        if not args.limit and not args.fixture and len(items) < expected * 0.5:
            print(f"  WARNING: expected ~{expected}, got {len(items)}. "
                  f"Check that {pool['label']} still selects this pool.",
                  file=sys.stderr)
        # A few items hold both designations. Keep the first and say so, rather
        # than letting the second silently overwrite it and mislabel its type.
        for q, it in items.items():
            if q in all_items:
                dual.append((q, all_items[q]["type"], kind))
            else:
                all_items[q] = it
    # Which source placed each item. Worth printing: if a fallback ever starts
    # carrying the bulk of the pool, that is a modelling change on Wikidata's
    # side and the ranking above should be revisited.
    by_source = {}
    for it in all_items.values():
        key = (it["type"], it.get("country_source") or "none")
        by_source[key] = by_source.get(key, 0) + 1
    print("\nCountry source:")
    for (kind, src), n in sorted(by_source.items()):
        print(f"  {kind:<11} {src:<13} {n:>5}")

    if dual:
        print(f"\n{len(dual)} item(s) carry both designations; kept the first:")
        for q, kept, skipped in dual[:10]:
            print(f"  {q}: kept {kept}, skipped {skipped}")

    if not args.keep_unofficial:
        print("\nFiltering to the official list...")
        all_items = filter_to_official(all_items)

    print("\nResolving countries...")
    countries = resolve_countries(all_items)

    # The guess is a point on the map, so the answer has to be somewhere a
    # finger can land. Wikidata's P17 is not always a modern state: it returns
    # the Ottoman Empire, the Soviet Union, Shu, and the odd Japanese town.
    # Those cannot be pointed at, so they are not answers -- an ISO 3166-1 code
    # is the test, since that is also what the map's polygons are keyed by.
    placeable = {q for q, r in countries.items() if r.get("iso")}
    dropped_hist = unplaceable = 0
    for it in list(all_items.values()):
        before = it.get("country_qids") or []
        kept = [q for q in before if q in placeable]
        if len(kept) != len(before):
            dropped_hist += len(before) - len(kept)
        it["country_qids"] = kept
        it["country_qid"] = kept[0] if kept else None
        if not kept:
            # No country anyone could point at: the round would have no
            # answer. Better absent than unanswerable.
            del all_items[it["qid"]]
            unplaceable += 1
    print(f"\n  dropped {dropped_hist} non-modern country statement(s); "
          f"{unplaceable} item(s) had no placeable country and were removed")

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
        print("\nResolving photos..." if not args.download_images
              else "\nDownloading photos...")
        resolve_photos(dataset, all_items, download=args.download_images)

    dataset.sort(key=lambda e: -e["sitelinks"])  # fame-ranked, highest first

    # Countries are the guessing vocabulary, so each one needs a coordinate:
    # the game measures every guess from the country's centroid to the target.
    # A country with no coordinate can't be guessed, so it's counted and named.
    country_table = {}
    no_coord = []
    for q, r in countries.items():
        if not (r.get("en") or r.get("fr") or r.get("es")):
            continue
        # Same rule as above: the vocabulary is exactly what the map can
        # resolve a tap to, so an entry here without an ISO code would be a
        # country the player is offered but can never point at.
        if not r.get("iso"):
            continue
        rec = {"en": r.get("en") or q,
               "fr": r.get("fr") or r.get("en") or q,
               "es": r.get("es") or r.get("en") or q}
        # The game tells you whether a guess was on the right continent, so each
        # country needs one of its own to compare against the target's.
        cont = continent_code([r["continent"]]) if r.get("continent") else None
        if cont:
            rec["cont"] = cont
        if r.get("iso"):
            rec["iso"] = r["iso"]
        if r.get("coord"):
            rec["lat"], rec["lng"] = round(r["coord"][0], 4), round(r["coord"][1], 4)
        else:
            no_coord.append(rec["en"])
        country_table[q] = rec
    if no_coord:
        print(f"  {len(no_coord)} countries have no coordinate and can't be guessed: "
              f"{', '.join(sorted(no_coord)[:8])}", file=sys.stderr)

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
