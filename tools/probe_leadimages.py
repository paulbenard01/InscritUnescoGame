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
    # formatversion=2 answers with a list instead of a keyed object.
    seq = list(pages.values()) if isinstance(pages, dict) else list(pages)
    withimg = [p_.get("title") for p_ in seq
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
        print(json.dumps(sample, ensure_ascii=False, indent=2)[:900])

    # The request pattern is not the suspect any more, so run the real function
    # over a realistic batch: full fifty titles, accents, parentheses, scripts.
    # If this returns nothing while the calls above return everything, the fault
    # is in the code around the request, not the request.
    print("\n== build_dataset.wikipedia_images(), the real thing ==")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import build_dataset as bd
    batch_en = (TITLES * 5)[:47] + ["Şəbi-hicran", "Tinku", "Al-Ayyala"]
    batch_fr = ["Flamenco", "Tango argentin", "Fest-noz", "Gwoka",
                "Repas gastronomique des Français"]
    got = bd.wikipedia_images({"en": batch_en, "fr": batch_fr})
    print(f"  asked about {len(set(batch_en))} en + {len(batch_fr)} fr titles; "
          f"got {len(got)} lead photographs")
    for k, v in list(got.items())[:5]:
        print(f"    {k} -> {v}")
    if not got:
        print("  NOTHING CAME BACK -- the fault is inside wikipedia_images(), "
              "not in the API request it makes.")

    # Same titles, same parameters, same headers as the pipeline -- printed
    # raw. Inference has run out: the function reports no exception and no API
    # error yet collects nothing, so the answer has to be in the response.
    print("\n== the pipeline's own request, verbatim ==")
    r = requests.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "prop": "pageimages",
        "piprop": "original", "pilicense": "any",
        "pilimit": 50,
        "titles": "|".join(TITLES), "format": "json",
    }, headers=bd.HEADERS, timeout=30)
    print(f"  User-Agent: {bd.HEADERS['User-Agent']}")
    print(f"  HTTP {r.status_code}  {r.url[:150]}")
    print("  body: " + r.text[:700].replace("\n", " "))

    print("\nVerdict above: whichever of these returns nothing is the bug.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
