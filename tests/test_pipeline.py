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
