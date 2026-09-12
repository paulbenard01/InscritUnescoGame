"""Unit checks for the parts of build_dataset.py that read Wikimedia's answers.

Offline and fast. These exist because every bug in this area was silent: the
pipeline kept running, printed a plausible number, and shipped less than it
should have.

    python tests/test_pipeline.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_dataset as bd

FAILS = []


def check(cond, label, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label +
          (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def main():
    print("== commons_filename ==")
    # The one that cost 349 entries. Wikipedia's pageimages API attaches
    # tracking parameters to the image URL, so the file name used to come out
    # as "GoshawkFalconry.jpg?utm_source=en.wikipedia.org&utm_campaign=api&
    # utm_content=original" -- which is_photo() rejected, silently, for every
    # article it looked up.
    utm = ("https://upload.wikimedia.org/wikipedia/commons/0/0b/"
           "GoshawkFalconry.jpg?utm_source=en.wikipedia.org"
           "&utm_campaign=api&utm_content=original")
    got = bd.commons_filename(utm)
    check(got == "GoshawkFalconry.jpg",
          "tracking parameters are stripped from the file name", got)
    check(bd.is_photo(got), "and the result is recognised as a photograph")

    check(bd.commons_filename(
        "http://commons.wikimedia.org/wiki/Special:FilePath/"
        "Petra%20Jordan%20BW%201.JPG") == "Petra Jordan BW 1.JPG",
        "percent-encoding is decoded, as Wikidata's P18 needs")
    check(bd.commons_filename(
        "https://upload.wikimedia.org/wikipedia/commons/8/8d/Belen_maya.jpg")
        == "Belen maya.jpg", "underscores become spaces")
    check(bd.commons_filename(
        "https://example.org/a/b/File.jpg#preview") == "File.jpg",
        "a fragment is stripped too")
    check(bd.commons_filename("") == "" and bd.commons_filename(None) == "",
          "an empty or missing URL yields no name rather than an error")

    print("\n== is_photo ==")
    for name in ("Petra.jpg", "Petra.JPEG", "x.png", "x.webp", "x.tif"):
        check(bd.is_photo(name), f"{name} counts as a photograph")
    # 46 Commons locator maps once shipped as "photographs"; an SVG map of the
    # country prints the answer on the board.
    for name in ("Locator map.svg", "Anthem.ogg", "Dance.webm", "Doc.pdf",
                 "Song.wav"):
        check(not bd.is_photo(name), f"{name} does not")

    print("\n== the brand files ==")
    # Enforced rather than remembered. A viewBox was added to these SVGs once,
    # and the next export from the design tool replaced the file and dropped it
    # again -- silently, because Chromium scales a viewBox-less SVG in an <img>
    # and Safari does not. Most players are on a phone, so this check exists to
    # fail the moment a file comes back without one.
    import glob
    import re as _re
    svgs = sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "*.svg")))
    check(bool(svgs), "there are brand SVGs to check", str(svgs))
    for path in svgs:
        name = os.path.basename(path)
        body = open(path, encoding="utf-8").read()
        root = _re.search(r"<svg[^>]*>", body)
        check(bool(root), f"{name}: has an <svg> root")
        if not root:
            continue
        head = root.group(0)
        vb = _re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
        check(bool(vb), f"{name}: has a viewBox, so it scales in Safari too",
              head[:120])
        wh = _re.search(r'width="([\d.]+)"[^>]*height="([\d.]+)"', head)
        if vb and wh:
            # A viewBox that disagrees with the drawing's own size would crop
            # or letterbox it, which is worse than not having one.
            check(abs(float(vb.group(1)) - float(wh.group(1))) < 0.01
                  and abs(float(vb.group(2)) - float(wh.group(2))) < 0.01,
                  f"{name}: and it matches the artwork's dimensions",
                  f"viewBox {vb.group(1)}x{vb.group(2)} vs {wh.group(1)}x{wh.group(2)}")

    print("\n== resolve_titles_from_response ==")
    # Everything the API can answer with, in one response: a title spelled
    # differently, one that moved, one that is gone, and one that is fine.
    body = {"query": {
        "normalized": [{"from": "mount fuji", "to": "Mount Fuji"},
                       {"from": "kimchi", "to": "Kimchi"}],
        "redirects": [{"from": "Kimchi", "to": "Kimjang"}],
        "pages": {
            "1": {"title": "Mount Fuji"},
            "2": {"title": "Kimjang"},
            "3": {"title": "Petra"},
            "-1": {"title": "No Such Article Anywhere", "missing": ""},
        }}}
    asked = ["mount fuji", "kimchi", "Petra", "No Such Article Anywhere"]
    got = bd.resolve_titles_from_response(body, asked)
    check(got.get("mount fuji") == "Mount Fuji",
          "a differently spelled title resolves to the real one", str(got))
    # Two hops: normalised, then followed to where the article actually lives.
    check(got.get("kimchi") == "Kimjang",
          "a redirect is followed to the article it points at", str(got))
    check(got.get("Petra") == "Petra", "a title that is already right is kept")
    check("No Such Article Anywhere" not in got,
          "a title with no article is dropped rather than linked", str(got))
    check(len(got) == 3, "and nothing else is invented", str(got))

    # A renamed title that points at an article which is itself missing must
    # not be kept: following the chain is not the same as landing somewhere.
    body2 = {"query": {"redirects": [{"from": "Old Name", "to": "New Name"}],
                       "pages": {"-1": {"title": "New Name", "missing": ""}}}}
    check(bd.resolve_titles_from_response(body2, ["Old Name"]) == {},
          "a redirect to a missing article is not a link")
    # A redirect loop must terminate rather than spin.
    body3 = {"query": {"redirects": [{"from": "A", "to": "B"},
                                     {"from": "B", "to": "A"}],
                       "pages": {"1": {"title": "C"}}}}
    check(bd.resolve_titles_from_response(body3, ["A"]) == {},
          "a circular redirect resolves to nothing instead of hanging")
    check(bd.resolve_titles_from_response({}, ["Petra"]) == {},
          "an empty response yields no links")

    print("\n== canon_title ==")
    check(bd.canon_title("Mount_Fuji") == bd.canon_title("mount fuji"),
          "titles match across underscores and case")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
