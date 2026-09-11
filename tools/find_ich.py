"""Find the Wikidata designation that actually marks intangible heritage.

The pipeline was originally pointed at Q1459900 for intangible heritage. That
turned out to be the *tentative* World Heritage list: it returns 1,745 items
against 849 official elements, and the entries are places -- Roman ruins,
national parks, a geological stratotype -- rather than living traditions. The
intangible pool is disabled in build_dataset.py until the right value is known.

An earlier probe tried to answer this by hand-picking a few QIDs believed to be
intangible elements and reading their P1435 values. That was a guess dressed up
as evidence: the sample items were not inscribed elements, so it found nothing.

This version guesses nothing. It asks Wikidata's own search for candidates,
then tests each candidate against facts:

  1. How many items carry it as a heritage designation (P1435)? The official
     register holds 788 elements across three lists; a pool an order of
     magnitude off is the wrong pool.
  2. Is there a dedicated identifier property for these elements? An item that
     carries an official element ID is inscribed by definition, so counting
     those is a second, independent read on the same number.
  3. What do the members actually look like? Traditions are events, practices
     and art forms. A pool whose members carry coordinates and are instances of
     "archaeological site" is a list of places, whatever it is called.

Point 3 is the one that caught the original mistake, so it is reported for
every candidate, not just the winner.

Run:  python tools/find_ich.py
"""
import json
import os
import sys

import requests

CONTACT = os.environ.get("HERITLE_CONTACT", "paulbenard01@gmail.com")
HEADERS = {"User-Agent": f"Heritle-audit/1.0 ({CONTACT}; designation discovery)"}
SPARQL_URL = "https://query.wikidata.org/sparql"
API_URL = "https://www.wikidata.org/w/api.php"
TIMEOUT = 90

# The size of the official register, used only as a yardstick. 849 is the
# figure the rest of this repo works from -- it comes from the official list
# supplied for the project, not from a number recalled from memory. Treat it as
# approximate: the register grows at each committee session.
OFFICIAL_TOTAL = 849

SEARCH_TERMS = [
    "intangible cultural heritage",
    "Representative List of the Intangible Cultural Heritage of Humanity",
    "List of Intangible Cultural Heritage in Need of Urgent Safeguarding",
    "Register of Good Safeguarding Practices",
    "masterpiece of the oral and intangible heritage of humanity",
]

COUNT_QUERY = "SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE { ?item wdt:P1435 wd:%s . }"

# What the members are: instance-of classes, how many carry coordinates, and a
# sample of names. Places have coordinates and are instances of site types;
# traditions generally have neither.
SHAPE_QUERY = """
SELECT ?kindLabel (COUNT(DISTINCT ?item) AS ?n) WHERE {
  ?item wdt:P1435 wd:%s .
  ?item wdt:P31 ?kind .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?kindLabel
ORDER BY DESC(?n)
LIMIT 8
"""
COORD_QUERY = """
SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE {
  ?item wdt:P1435 wd:%s . ?item wdt:P625 ?c .
}
"""
SAMPLE_QUERY = """
SELECT ?itemLabel WHERE {
  ?item wdt:P1435 wd:%s .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 12
"""
# Items carrying a given identifier property, as an independent count.
PROP_COUNT_QUERY = "SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE { ?item wdt:%s ?v . }"


def sparql(query):
    r = requests.post(SPARQL_URL, data={"query": query, "format": "json"},
                      headers={**HEADERS, "Accept": "application/sparql-results+json"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def scalar(query, default=None):
    try:
        rows = sparql(query)
        return int(rows[0]["n"]["value"]) if rows else default
    except Exception as exc:
        print(f"    query failed: {exc}", file=sys.stderr)
        return default


def search(term, kind="item", limit=8):
    """Ask Wikidata's search for candidates, rather than supplying our own."""
    try:
        r = requests.get(API_URL, headers=HEADERS, timeout=TIMEOUT, params={
            "action": "wbsearchentities", "search": term, "language": "en",
            "type": kind, "limit": limit, "format": "json",
        })
        r.raise_for_status()
        return [(h["id"], h.get("label", ""), h.get("description", ""))
                for h in r.json().get("search", [])]
    except Exception as exc:
        print(f"  search for {term!r} failed: {exc}", file=sys.stderr)
        return []


def main():
    print("=" * 72)
    print("WHICH WIKIDATA DESIGNATION MARKS INTANGIBLE HERITAGE?")
    print(f"official register: about {OFFICIAL_TOTAL} elements")
    print("=" * 72)

    # ---- identifier properties: an independent read on the same number ----
    print("\nIdentifier properties matching the search:")
    props = {}
    for term in SEARCH_TERMS[:1] + ["intangible heritage element ID"]:
        for pid, label, desc in search(term, kind="property", limit=6):
            props.setdefault(pid, (label, desc))
    if not props:
        print("  none found")
    for pid, (label, desc) in props.items():
        n = scalar(PROP_COUNT_QUERY % pid, "?")
        close = isinstance(n, int) and abs(n - OFFICIAL_TOTAL) < 150
        flag = "  <-- close to the register" if close else ""
        print(f"  {pid:8} {label[:44]:<44} {str(n):>6} items{flag}")
        if desc:
            print(f"           {desc[:66]}")

    # ---- candidate designation values ----
    candidates = {}
    for term in SEARCH_TERMS:
        for qid, label, desc in search(term):
            candidates.setdefault(qid, (label, desc))
    # The known-wrong value, included deliberately so the comparison is visible.
    candidates.setdefault("Q1459900", ("(the value the pipeline had)", ""))

    print(f"\n{len(candidates)} candidate designations. Counting members:\n")
    scored = []
    for qid, (label, desc) in sorted(candidates.items()):
        n = scalar(COUNT_QUERY % qid, 0)
        if not n:
            continue
        coords = scalar(COORD_QUERY % qid, 0) or 0
        scored.append((qid, label, desc, n, coords))

    scored.sort(key=lambda r: -r[3])
    for qid, label, desc, n, coords in scored:
        pct = 100 * coords // n if n else 0
        print(f"  {qid:11} {label[:40]:<40} {n:>6} items, {pct:>3}% have coordinates")
        if desc:
            print(f"              {desc[:64]}")

    print("\n" + "-" * 72)
    print("What the members look like (places have coordinates and site classes;")
    print("traditions do not):")
    for qid, label, desc, n, coords in scored[:4]:
        print(f"\n  {qid} — {label}  [{n} items]")
        try:
            kinds = sparql(SHAPE_QUERY % qid)
        except Exception as exc:
            print(f"    shape query failed: {exc}", file=sys.stderr)
            kinds = []
        for row in kinds[:6]:
            print(f"    instance of: {row['kindLabel']['value'][:44]:<44} "
                  f"{row['n']['value']:>5}")
        try:
            names = [r["itemLabel"]["value"] for r in sparql(SAMPLE_QUERY % qid)]
        except Exception:
            names = []
        for name in names[:8]:
            print(f"      · {name[:60]}")

    print("\n" + "=" * 72)
    print(f"Read it this way: the right value has roughly {OFFICIAL_TOTAL} "
          "members, few coordinates,")
    print("and members that are practices and festivals rather than sites.")
    print("Nothing here is applied automatically — build_dataset.py keeps the")
    print("intangible pool disabled until a human confirms the value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
