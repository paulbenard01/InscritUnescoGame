"""Full-flow smoke test: one win, one deliberate loss, one solve.

Walks the exact path a player takes and asserts the end state, at both a
375px phone width and a desktop width, with the browser console watched
throughout. Uses ?day=N so the run is deterministic instead of depending on
what today happens to be.

    python tests/smoke.py                 # synthetic dataset (offline)
    python tests/smoke.py --real          # the committed data/dataset.json
    python tests/smoke.py --url https://…/  # a deployed build

Exit code is non-zero if anything fails.
"""
import argparse
import glob
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
DAYS = [412, 900, 1301]          # arbitrary but fixed, so runs are comparable


def check(cond, label, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def serve(root):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw): super().__init__(*a, directory=root, **kw)
        def log_message(self, *a): pass
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def play_day(page, base, day, width, label):
    """One full day: round 1 won, round 2 lost on purpose, round 3 solved."""
    errors, bad_requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # A missing photo is expected and handled by the placeholder; a missing
    # script or data file is not. Google Fonts is excluded because this sandbox
    # blocks it at the proxy — the page falls back to system fonts here, which
    # says nothing about a real browser.
    def interesting(url, kind):
        if "fonts.googleapis.com" in url or "fonts.gstatic.com" in url:
            return False
        return kind in ("script", "stylesheet", "document", "fetch", "xhr")
    page.on("requestfailed", lambda r: bad_requests.append(r.url)
            if interesting(r.url, r.resource_type) else None)
    page.on("response", lambda r: bad_requests.append(f"{r.status} {r.url}")
            if r.status >= 400 and interesting(r.url, r.request.resource_type) else None)

    page.goto(f"{base}?day={day}" if day is not None else base)
    page.wait_for_function("typeof POOL !== 'undefined' && POOL.length > 0", timeout=15000)
    page.evaluate("document.getElementById('fnClose').click()")   # dismiss Field Notes
    page.wait_for_timeout(200)

    if day is not None:
        check(page.evaluate("dayIndex") == day, f"{label}: ?day={day} overrides the date",
              str(page.evaluate("dayIndex")))
    else:
        check(page.evaluate("dayIndex === todayIndex && !isPractice"),
              f"{label}: no ?day means today, and it counts")
    check(page.evaluate("targets.length") == 3, f"{label}: three targets chosen")

    # ---- every language renders ----
    for code, needle in (("fr", "devinettes"), ("es", "adivinanzas"), ("en", "heritage")):
        page.click(f".lang-btn[data-lang='{code}']")
        page.wait_for_timeout(120)
        check(needle in page.locator("#tagline").inner_text().lower(),
              f"{label}: {code.upper()} renders")

    # ---- round 1: win ----
    page.evaluate("submitGuess(targetCountry())")
    page.wait_for_timeout(250)
    check(page.evaluate("state.roundStatus[0]") == "solved", f"{label}: round 1 won")
    page.evaluate("document.getElementById('skipBonusBtn')?.click()")
    page.wait_for_timeout(150)
    page.click("#nextBtn"); page.wait_for_timeout(250)

    # ---- round 2: six wrong guesses, on purpose ----
    wrongs = page.evaluate("""
      () => COUNTRIES.filter(c => c.names.en !== targets[1].country.en)
                     .slice(0, 6).map(c => c.id)
    """)
    check(len(wrongs) == 6, f"{label}: six wrong countries available", str(len(wrongs)))
    for cid in wrongs:
        page.evaluate("id => submitGuess(COUNTRIES.find(c => c.id === id))", cid)
        page.wait_for_timeout(60)
    check(page.evaluate("state.roundStatus[1]") == "failed", f"{label}: round 2 lost",
          str(page.evaluate("state.roundStatus[1]")))
    check(page.evaluate("state.roundScore[1]") == 0, f"{label}: a lost round scores 0")
    page.click("#nextBtn"); page.wait_for_timeout(250)

    # ---- round 3: solve ----
    page.evaluate("submitGuess(targetCountry())")
    page.wait_for_timeout(250)
    check(page.evaluate("state.roundStatus[2]") == "solved", f"{label}: round 3 solved")
    page.evaluate("document.getElementById('skipBonusBtn')?.click()")
    page.wait_for_timeout(150)
    page.click("#nextBtn"); page.wait_for_timeout(400)

    # ---- final screen: 1 win / 1 loss / 1 solved ----
    check(not page.locator("#finalResult").is_hidden(), f"{label}: final screen shown")
    grid = page.locator("#finalResult .share-grid").inner_text()
    check("—" in grid or len(grid.strip()) > 0, f"{label}: share grid rendered", repr(grid))
    check(grid.count("\n") == 2, f"{label}: share grid has one line per round", repr(grid))
    score = page.evaluate("totalScore()")
    check(0 < score < 100, f"{label}: score reflects one lost round", str(score))
    statuses = page.evaluate("state.roundStatus")
    check(statuses == ["solved", "failed", "solved"],
          f"{label}: 1 win / 1 loss / 1 solved", str(statuses))

    check(not errors, f"{label}: no console errors", "; ".join(errors[:3]))
    check(not bad_requests, f"{label}: no broken script/data requests",
          "; ".join(bad_requests[:3]))
    return errors, bad_requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="test a deployed build instead of a local copy")
    ap.add_argument("--real", action="store_true",
                    help="serve the committed data/dataset.json rather than synthetic data")
    args = ap.parse_args()

    httpd = root = None
    if args.url:
        base = args.url if args.url.endswith("/") else args.url + "/"
        print(f"testing deployed build at {base}")
    else:
        root = tempfile.mkdtemp(prefix="heritle-smoke-")
        shutil.copy(os.path.join(REPO, "heritle.html"), os.path.join(root, "index.html"))
        if args.real:
            shutil.copytree(os.path.join(REPO, "data"), os.path.join(root, "data"))
            print("serving the committed dataset")
        else:
            subprocess.run([sys.executable, os.path.join(REPO, "tests", "make_synthetic_dataset.py"),
                            os.path.join(root, "data", "dataset.json")],
                           check=True, stdout=subprocess.DEVNULL)
        httpd, port = serve(root)
        base = f"http://127.0.0.1:{port}/"

    chrome = os.environ.get("CHROME_BIN") or next(
        (p for p in glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")), None)
    launch = {"args": ["--no-sandbox"]}
    if chrome and os.path.exists(chrome):
        launch["executable_path"] = chrome

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        for label, vp, mobile in [("mobile 375", {"width": 375, "height": 812}, True),
                                  ("desktop 1280", {"width": 1280, "height": 900}, False)]:
            print(f"\n== {label} ==")
            for day in DAYS[:1] if args.url else DAYS:
                ctx = browser.new_context(viewport=vp, has_touch=mobile, is_mobile=mobile)
                page = ctx.new_page()
                play_day(page, base, day, vp["width"], f"{label} d{day}")
                ctx.close()

        # ---- the collection, passport and archive after a completed day ----
        print("\n== collection / passport / archive ==")
        ctx = browser.new_context(viewport={"width": 375, "height": 812},
                                  has_touch=True, is_mobile=True)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        play_day(page, base, None, 375, "profile")   # today: the scoring path
        page.evaluate("showView('Collection')"); page.wait_for_timeout(300)
        cards = page.locator("#viewCollection .card").count()
        check(cards >= 2, "solved entries are catalogued", str(cards))
        acc = page.locator("#viewCollection .acc").first.inner_text()
        check(acc.startswith("HTL."), "cards carry an accession number", acc)
        page.evaluate("showView('Passport')"); page.wait_for_timeout(300)
        check(page.locator("#viewPassport .stamp").count() >= 1, "passport shows a stamp")
        check(page.locator("#viewPassport .ach").count() == 5, "five distinctions listed")
        page.evaluate("showView('Archive')"); page.wait_for_timeout(300)
        check(page.locator("#viewArchive .arch-row").count() > 5, "archive lists past days")
        # colour-blind toggle
        page.evaluate("showView('Passport')"); page.wait_for_timeout(200)
        page.check("#cbToggle"); page.wait_for_timeout(200)
        check(page.evaluate("document.body.classList.contains('cb')"),
              "colour-blind marks toggle on")
        check(not errors, "no console errors across the panels", "; ".join(errors[:3]))
        ctx.close()
        browser.close()

    if httpd:
        httpd.shutdown()
    if root:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + ("SMOKE PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS[:6]}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
