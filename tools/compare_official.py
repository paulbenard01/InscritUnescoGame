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
# The export exists per language, which matters: it is a source of official
# FR/ES titles for every site, where Wikidata's label coverage is partial.
WHS_CANDIDATES = [
    "https://whc.unesco.org/{lang}/list/xml/",
    "https://whc.unesco.org/{lang}/list/xml",
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


def parent_site_id(sid):
    """Reduce a site number to the parent inscription.

    Wikidata's P757 values carry suffixes the official export doesn't: revision
    markers ("1153rev"), re-inscriptions ("1bis"), component numbers ("813-001")
    and re-nomination years ("1558-2023"). All of those describe the same
    inscription as their numeric stem, so a raw string join reports them as
    disagreements when they are nothing of the sort.
    """
    m = re.match(r"(\d+)", str(sid).strip())
    return m.group(1) if m else None


def is_component(sid):
    """True for a component/revision of a larger inscription rather than the
    inscription itself -- these are what put things like 'Humble
    Administrator's Garden' in the list beside 'Classical Gardens of Suzhou'."""
    return bool(re.match(r"^\d+[-a-z]", str(sid).strip()))


def slugify(name):
    """Match the id scheme build_dataset.py uses, so our ids can be compared to
    the slugs in official element URLs."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def norm(name):
    """Fold a title to something comparable: ASCII, lowercase, alphanumeric."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fetch_official_whs(lang="en"):
    """Return {official_id: title} from the official export in one language."""
    for template in WHS_CANDIDATES:
        url = template.format(lang=lang)
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
            print(f"  official export [{lang}]: {url} -> {len(sites)} sites")
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
            names.add((m.group(2), m.group(1)))  # (inscription id, url slug)
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
    official_by_lang = {}
    for lang in ("en", "fr", "es"):
        got = fetch_official_whs(lang)
        if got:
            official_by_lang[lang] = got
    official_whs = official_by_lang.get("en", {})
    official_ich = fetch_official_ich()
    print()

    # ---- World Heritage Sites: exact join on the official site number --------
    # Bound up front: the trilingual section below reads them, and would raise
    # a NameError if the official export could not be fetched.
    qid2sid, official_ids = {}, set()
    if official_whs:
        print("=" * 66)
        print("WORLD HERITAGE SITES (matched on official site number via P757)")
        print("=" * 66)
        qid2sid = fetch_qid_to_site_id()
        ours_by_sid, no_sid = {}, []
        for e in ours_material:
            sid = qid2sid.get(e["qid"])
            (ours_by_sid.setdefault(sid, e) if sid else no_sid.append(e))

        official_ids = {parent_site_id(s) for s in official_whs} - {None}
        official_by_parent = {}
        for sid, title in official_whs.items():
            official_by_parent.setdefault(parent_site_id(sid), title)

        our_parents, components = set(), []
        for sid, e in ours_by_sid.items():
            p = parent_site_id(sid)
            if p:
                our_parents.add(p)
            if is_component(sid):
                components.append((sid, e))

        missing = official_ids - our_parents
        extra = our_parents - official_ids

        print(f"official inscriptions:   {len(official_ids)}")
        print(f"ours carrying a site id: {len(ours_by_sid)} "
              f"({len(our_parents)} distinct inscriptions)")
        print(f"ours with no site id:    {len(no_sid)}")
        print(f"COVERAGE:                {100 * len(our_parents & official_ids) / max(len(official_ids), 1):.1f}% "
              f"of the official list")
        # "813-001" is a component of a serial site; "1bis"/"1153rev" are
        # re-inscriptions or boundary revisions of one. Different things, both
        # meaning the entry is not the plain inscription the official list names.
        print(f"components or revisions in ours: {len(components)}")
        for sid, e in sorted(components)[:8]:
            print(f"  {sid:10} {e['names']['en'][:52]}")

        print(f"\nofficial inscriptions missing from ours: {len(missing)}")
        for sid in sorted(missing, key=lambda x: int(x))[:args.show]:
            print(f"  {sid}  {official_by_parent[sid]}")
        if len(missing) > args.show:
            print(f"  ... and {len(missing) - args.show} more")
        print(f"\nsite ids in ours with no official counterpart: {len(extra)}")
        for sid in sorted(extra, key=lambda x: int(x))[:args.show]:
            print(f"  {sid}")
        if no_sid:
            print(f"\nours with no site id on Wikidata (sample):")
            for e in no_sid[:args.show]:
                print(f"  {e['qid']}  {e['names']['en']}")
    else:
        print("Could not retrieve the official World Heritage export — see errors "
              "above. No material comparison possible.", file=sys.stderr)

    # ---- What the official titles could add to our translations -------------
    if len(official_by_lang) > 1 and official_ids:
        print()
        print("=" * 66)
        print("TRILINGUAL NAMES AVAILABLE FROM THE OFFICIAL LIST")
        print("=" * 66)
        for lang in ("en", "fr", "es"):
            n = len(official_by_lang.get(lang, {}))
            print(f"  {lang}: {n} official titles")
        # How many of our material entries currently fall back to English?
        fellback = [e for e in ours_material
                    if e["names"]["fr"] == e["names"]["en"]
                    or e["names"]["es"] == e["names"]["en"]]
        joinable = [e for e in fellback
                    if parent_site_id(qid2sid.get(e["qid"], "")) in official_ids]
        print(f"\n  our material entries falling back to English in fr or es: {len(fellback)}")
        print(f"  of those, matchable to an official inscription: {len(joinable)}")
        print("\n  So the official export could supply real FR/ES titles for those,")
        print("  where Wikidata has none. Note the licensing difference before")
        print("  shipping it: Wikidata is CC0, the official list is not.")

    # ---- Intangible elements: name comparison only, and approximate ----------
    print()
    if official_ich:
        print("=" * 66)
        print("INTANGIBLE ELEMENTS (approximate: normalised-name match only)")
        print("=" * 66)
        # Compare our ids to the official URL slugs -- both are slugified
        # English titles, so this is far closer to like-for-like than comparing
        # a slug against a full label.
        official_slugs = set(official_ich.values())
        our_slugs = {slugify(e["names"]["en"]) for e in ours_immaterial}
        exact = our_slugs & official_slugs
        # Prefix matches catch the common case where one side truncates.
        loose = {s for s in our_slugs - exact
                 if any(o.startswith(s[:24]) or s.startswith(o[:24]) for o in official_slugs)}
        print(f"official elements: {len(official_ich)}")
        print(f"ours:              {len(ours_immaterial)}")
        print(f"slug matches:      {len(exact)} exact + {len(loose)} partial "
              f"= {100 * (len(exact) + len(loose)) / max(len(our_slugs), 1):.1f}% of ours")
        print("\nThis half has no official machine-readable export, so it is matched on")
        print("slugified titles rather than identifiers. Treat it as indicative: a miss")
        print("is as likely to be different wording as a genuinely absent element.")
    else:
        print("Could not retrieve the official intangible listing — see errors above.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
