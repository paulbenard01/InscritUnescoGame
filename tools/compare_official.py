"""Compare the Wikidata-derived dataset against the official inscription lists.

Development tool, not part of the game. It answers one question: how much of
the official record did the Wikidata pipeline actually capture?

Matching is done on official identifiers, not names. Wikidata labels and
official titles diverge constantly ("Historic Centre of Rome, the Properties of
the Holy See..." vs "Historic Centre of Rome"), so name matching would invent
disagreements that aren't there. World Heritage Sites carry P757 on Wikidata,
which is the official site number, so that join is exact. Where no identifier
exists on either side the script falls back to normalised names and says so.

Run:  python tools/compare_official.py --dataset data/dataset.json
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

import requests

CONTACT = os.environ.get("UNESCLE_CONTACT", "paulbenard01@gmail.com")
HEADERS = {"User-Agent": f"Unescle-audit/1.0 ({CONTACT}; dataset completeness check)"}
SPARQL_URL = "https://query.wikidata.org/sparql"
TIMEOUT = 90

# The official World Heritage list has a machine-readable export. The exact path
# has moved between site revisions, so try the known forms and report which one
# answered rather than assuming.
WHS_CANDIDATES = [
    "https://whc.unesco.org/en/list/xml/",
    "https://whc.unesco.org/en/list/xml",
    "https://whc.unesco.org/en/list/?&type=xml",
]
# The intangible list has no equivalent stable export; the browse pages are HTML.
ICH_CANDIDATES = [
    "https://ich.unesco.org/en/lists?text=&multinational=3&display1=inscriptionID#tabs",
    "https://ich.unesco.org/en/lists",
]

# Every Wikidata item holding a World Heritage site number, so our qids can be
# joined to official ids exactly.
P757_QUERY = """
SELECT ?item ?sid WHERE { ?item wdt:P757 ?sid . }
"""


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)


def norm(name):
    """Fold a title to something comparable: ASCII, lowercase, alphanumeric."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fetch_official_whs():
    """Return {official_id: title} from the official World Heritage export."""
    for url in WHS_CANDIDATES:
        try:
            r = get(url)
            if r.status_code != 200 or len(r.content) < 1000:
                print(f"  {url} -> HTTP {r.status_code}, {len(r.content)} bytes", file=sys.stderr)
                continue
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"  {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        sites = {}
        for row in root.iter():
            if row.tag.lower() not in ("row", "site"):
                continue
            fields = {c.tag.lower(): (c.text or "").strip() for c in row}
            sid = fields.get("id_number") or fields.get("id")
            title = fields.get("site") or fields.get("name_en") or fields.get("name")
            if sid and title:
                sites[str(sid).strip()] = title
        if sites:
            print(f"  official World Heritage export: {url} -> {len(sites)} sites")
            return sites
        print(f"  {url} parsed but yielded no rows", file=sys.stderr)
    return {}


def fetch_official_ich():
    """Scrape element names from the official intangible-heritage browse pages.

    There is no stable machine-readable export, so this is best-effort: it reads
    the element links off the listing page. Treated as approximate throughout.
    """
    for url in ICH_CANDIDATES:
        try:
            r = get(url)
            if r.status_code != 200:
                print(f"  {url} -> HTTP {r.status_code}", file=sys.stderr)
                continue
        except requests.RequestException as exc:
            print(f"  {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        # Element pages look like /en/RL/some-element-name-00123
        names = set()
        for m in re.finditer(r'/en/(?:RL|BSP|USL)/([a-z0-9\-]+?)-(\d{5})\b', r.text):
            names.add((m.group(2), m.group(1).replace("-", " ")))
        if names:
            print(f"  official intangible listing: {url} -> {len(names)} elements")
            return {sid: title for sid, title in names}
        print(f"  {url} yielded no element links", file=sys.stderr)
    return {}


def fetch_qid_to_site_id():
    """qid -> official World Heritage site number, straight from Wikidata."""
    r = requests.post(SPARQL_URL, data={"query": P757_QUERY, "format": "json"},
                      headers={**HEADERS, "Accept": "application/sparql-results+json"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for row in r.json()["results"]["bindings"]:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        out[qid] = str(row["sid"]["value"]).strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="data/dataset.json")
    ap.add_argument("--show", type=int, default=25, help="examples to print per bucket")
    args = ap.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        data = json.load(f)
    entries = data["entries"]
    ours_material = [e for e in entries if e["type"] == "material"]
    ours_immaterial = [e for e in entries if e["type"] == "immaterial"]
    print(f"our dataset: {len(entries)} entries "
          f"({len(ours_material)} material, {len(ours_immaterial)} intangible)\n")

    print("Fetching official lists...")
    official_whs = fetch_official_whs()
    official_ich = fetch_official_ich()
    print()

    # ---- World Heritage Sites: exact join on the official site number --------
    if official_whs:
        print("=" * 66)
        print("WORLD HERITAGE SITES (matched on official site number via P757)")
        print("=" * 66)
        qid2sid = fetch_qid_to_site_id()
        ours_by_sid, no_sid = {}, []
        for e in ours_material:
            sid = qid2sid.get(e["qid"])
            (ours_by_sid.setdefault(sid, e) if sid else no_sid.append(e))

        official_ids = set(official_whs)
        our_ids = set(ours_by_sid)
        missing = official_ids - our_ids
        extra = our_ids - official_ids

        print(f"official list:        {len(official_ids)}")
        print(f"ours with a site id:  {len(our_ids)}")
        print(f"ours without one:     {len(no_sid)} (can't be matched by id)")
        print(f"coverage:             {100 * len(our_ids & official_ids) / max(len(official_ids), 1):.1f}%")
        print(f"\nin the official list but missing from ours: {len(missing)}")
        for sid in sorted(missing)[:args.show]:
            print(f"  {sid}  {official_whs[sid]}")
        if len(missing) > args.show:
            print(f"  ... and {len(missing) - args.show} more")
        print(f"\nin ours but not in the official list: {len(extra)}")
        for sid in sorted(extra)[:args.show]:
            print(f"  {sid}  {ours_by_sid[sid]['names']['en']}")
        if len(extra) > args.show:
            print(f"  ... and {len(extra) - args.show} more")
        if no_sid:
            print(f"\nours with no site id on Wikidata (sample):")
            for e in no_sid[:args.show]:
                print(f"  {e['qid']}  {e['names']['en']}")
    else:
        print("Could not retrieve the official World Heritage export — see errors "
              "above. No material comparison possible.", file=sys.stderr)

    # ---- Intangible elements: name comparison only, and approximate ----------
    print()
    if official_ich:
        print("=" * 66)
        print("INTANGIBLE ELEMENTS (approximate: normalised-name match only)")
        print("=" * 66)
        official_names = {norm(t) for t in official_ich.values()}
        our_names = {norm(e["names"]["en"]) for e in ours_immaterial}
        hits = sum(1 for n in our_names if n in official_names)
        print(f"official elements scraped: {len(official_ich)}")
        print(f"ours:                      {len(ours_immaterial)}")
        print(f"exact normalised-name hits: {hits} "
              f"({100 * hits / max(len(our_names), 1):.1f}% of ours)")
        print("\nNames are a weak key here: the official titles and Wikidata labels")
        print("are worded differently far more often than they actually disagree, so")
        print("a low hit rate is not evidence of missing entries.")
    else:
        print("Could not retrieve the official intangible listing — see errors above.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
