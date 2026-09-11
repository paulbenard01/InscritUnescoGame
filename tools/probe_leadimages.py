"""Why does the Wikipedia lead-image fallback find nothing?

The full rebuild reported, unambiguously:

    344 entries still have nothing; trying 210 Wikipedia articles
    0 of those articles had a lead photograph

Zero out of two hundred and ten is not what "these articles have no picture"
looks like -- an article about a tradition nearly always leads with one. It is
what a rejected request looks like, and wikipedia_images() swallows the reason.

So this asks the API directly, prints what comes back, and varies one parameter
at a time. Run it in CI: the sandbox cannot reach Wikipedia.

    python tools/probe_leadimages.py
"""
import json
import os
import sys

import requests

CONTACT = os.environ.get("HERITLE_CONTACT", "heritle@example.invalid")
HEADERS = {"User-Agent": f"Heritle/1.0 ({CONTACT}) python-requests"}
API = "https://en.wikipedia.org/w/api.php"

# Articles that certainly have a lead photograph, so a zero is the request's
# fault and not the subject's.
TITLES = ["Petra", "Mount Fuji", "Flamenco", "Tango", "Yoga", "Kimchi",
          "Hopak", "Nowruz", "Falconry", "Whistled language"]


def call(label, params):
    p = {"action": "query", "format": "json", "prop": "pageimages"}
    p.update(params)
    try:
        r = requests.get(API, params=p, headers=HEADERS, timeout=30)
        body = r.json()
    except Exception as err:                       # noqa: BLE001 -- reporting
        print(f"{label:38s} REQUEST FAILED: {type(err).__name__}: {err}")
        return None
    if "error" in body:
        print(f"{label:38s} API ERROR: {body['error'].get('code')} — "
              f"{body['error'].get('info')}")
        return None
    if "warnings" in body:
        for mod, w in body["warnings"].items():
            print(f"{label:38s} warning [{mod}]: "
                  f"{' '.join(str(v) for v in w.values())[:160]}")
    pages = body.get("query", {}).get("pages", {})
    withimg = [p_.get("title") for p_ in pages.values()
               if (p_.get("original") or p_.get("thumbnail"))]
    print(f"{label:38s} HTTP {r.status_code}  pages={len(pages)}  "
          f"with an image={len(withimg)}")
    return pages


def main():
    titles = "|".join(TITLES)
    print(f"Asking {API} about {len(TITLES)} articles that certainly have "
          f"a lead photograph.\n")

    print("== what the pipeline sends ==")
    call("piprop=original, pilimit=50",
         {"piprop": "original", "pilicense": "any", "pilimit": 50,
          "titles": titles})

    print("\n== one parameter at a time ==")
    call("without pilimit", {"piprop": "original", "pilicense": "any",
                             "titles": titles})
    call("without pilicense", {"piprop": "original", "pilimit": 50,
                               "titles": titles})
    call("pilimit=50, no pilicense/original",
         {"piprop": "original", "pilimit": 50, "titles": titles})
    call("piprop=thumbnail|original",
         {"piprop": "thumbnail|original", "pilimit": 50, "titles": titles})
    call("one title, piprop=original",
         {"piprop": "original", "pilicense": "any", "titles": TITLES[0]})
    pages = call("formatversion=2",
                 {"piprop": "original", "pilicense": "any", "pilimit": 50,
                  "titles": titles, "formatversion": 2})

    if pages:
        print("\n== a sample page, verbatim ==")
        sample = list(pages.values() if isinstance(pages, dict) else pages)[:2]
        print(json.dumps(sample, ensure_ascii=False, indent=2)[:1200])

    print("\nVerdict: the combination above that reports images is the one the "
          "pipeline should send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
