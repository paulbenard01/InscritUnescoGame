"""End-to-end browser test: plays the game against a served dataset.json.

Covers the wiring the pipeline swap touches -- async load, per-tier day picks
from the full pool, trilingual clue tiles, image + credit, and the file://
fallback to the built-in demo set.
"""
import http.server, json, os, shutil, socketserver, subprocess, sys, tempfile, threading
from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []

# Serve a scratch copy rather than the repo itself. The checked-in dataset
# points its photos at Commons' CDN, which the test can neither rely on nor
# reach offline; the synthetic set ships local image files instead, so the
# photo paths are genuinely exercised and the run doesn't depend on the network
# or on whatever happens to be committed under data/.
ROOT = tempfile.mkdtemp(prefix="heritle-test-")
shutil.copy(os.path.join(REPO, "heritle.html"), ROOT)
subprocess.run([sys.executable, os.path.join(REPO, "tests", "make_synthetic_dataset.py"),
                os.path.join(ROOT, "data", "dataset.json")],
               check=True, stdout=subprocess.DEVNULL)
# The map geometry is independent of the dataset and real either way; without
# it the page silently falls back to its coarse built-in outline.
_land = os.path.join(REPO, "data", "land.json")
if os.path.exists(_land):
    shutil.copy(_land, os.path.join(ROOT, "data", "land.json"))

TAP_JS = """(pt) => {
  const p = project(pt.lat, pt.lng);
  const svg = document.getElementById('mapSvg');
  const r = svg.getBoundingClientRect();
  svg.dispatchEvent(new MouseEvent('click', {
    clientX: r.left + ((p.x - mapView.x) / mapView.w) * r.width,
    clientY: r.top  + ((p.y - mapView.y) / mapView.h) * r.height,
    bubbles: true }));
}"""


def tap_country(page, country_js):
    """Tap the map at a country's centroid and commit the guess."""
    pt = page.evaluate(f"() => {{ const c = {country_js}; return {{lat:c.lat, lng:c.lng}}; }}")
    page.evaluate(TAP_JS, pt)
    page.wait_for_timeout(200)
    page.click("#confirmGuess")
    page.wait_for_timeout(250)


def check(cond, label, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)

class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, *a): pass

def main():
    with socketserver.TCPServer(("127.0.0.1", 0), Quiet) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}/heritle.html"

        with sync_playwright() as pw:
            # Use the sandbox's pre-installed Chromium when present, otherwise let
            # Playwright resolve its own (CI installs one via `playwright install`).
            chrome = os.environ.get("CHROME_BIN") or next(
                (p for p in __import__("glob").glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")), None)
            launch = {"args": ["--no-sandbox"]}
            if chrome and os.path.exists(chrome):
                launch["executable_path"] = chrome
            browser = pw.chromium.launch(**launch)

            for label, viewport in [("desktop 1280x900", {"width":1280,"height":900}),
                                    ("mobile 390x844", {"width":390,"height":844})]:
                print(f"\n== {label} ==")
                ctx = browser.new_context(viewport=viewport)
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base); page.wait_for_timeout(700)
                # Field Notes covers the board on a first visit; each context is fresh.
                page.evaluate("document.getElementById('fnClose')?.click()")
                page.wait_for_timeout(120)

                pool = page.evaluate("POOL.length")
                check(pool > 2000, "loaded full dataset from data/dataset.json", f"POOL={pool}")
                plan = page.evaluate("targets.map(t=>({type:t.type,tier:t.tier}))")
                # The pool has to carry both kinds, or the bonus round quietly
                # falls back to a fourth site and nobody notices.
                check(page.evaluate("POOL.some(d=>d.type==='immaterial')"),
                      "dataset carries intangible entries")
                check([p["type"] for p in plan] == ["material"] * 3 + ["immaterial"],
                      "three heritage rounds then an intangible bonus", str(plan))
                check(sorted(p["tier"] for p in plan[:3]) == [1, 2, 3],
                      "difficulty ramps across the three heritage rounds", str(plan))
                check(plan[3]["tier"] == 1,
                      "the one-guess bonus round draws a widely known element", str(plan))
                ids = page.evaluate("targets.map(t=>t.id)")
                check(len(set(ids)) == 4, "four distinct targets", str(ids))

                check(page.locator(".wordmark").inner_text() == "Heritle", "wordmark intact")
                check(page.locator("#modeBadge").inner_text() != "", "mode badge rendered")
                check(page.locator(".clue").count() == 5, "five clue tiles")
                # Region replaced Country on the ladder (the country is now the
                # answer). It's derived from the coordinate, and a bug in that
                # derivation shows up only as the word "Unknown" on the tile.
                check("Country" not in page.locator("#clues").inner_text(),
                      "clue ladder no longer reveals the country")
                region = page.evaluate("regionOf(targets[0])")
                check(region and "Unknown" not in region and "?" not in region,
                      "region clue derives a real value", str(region))

                # No horizontal overflow at this width.
                over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                check(over <= 0, "no horizontal overflow", f"{over}px")

                # Language slider drives every visible string.
                page.click(".lang-btn[data-lang='fr']"); page.wait_for_timeout(150)
                fr = page.locator("#tagline").inner_text()
                check("chaque jour" in fr, "FR tagline", fr)
                page.click(".lang-btn[data-lang='es']"); page.wait_for_timeout(150)
                es = page.locator("#tagline").inner_text()
                check("cada día" in es, "ES tagline", es)
                page.click(".lang-btn[data-lang='en']"); page.wait_for_timeout(150)

                # Guesses are countries now, never site names.
                check(page.evaluate("COUNTRIES.length") > 10, "country list loaded",
                      str(page.evaluate("COUNTRIES.length")))
                # The map is the input: every country the game knows must be
                # reachable by pointing at it.
                check(page.evaluate("!!LAND_SHAPES"), "country shapes loaded for hit-testing")
                unreachable = page.evaluate("""
                  () => COUNTRIES.filter(c => !c.iso || !LAND_SHAPES[c.iso]).length
                """)
                check(unreachable == 0, "every guessable country is on the map",
                      f"{unreachable} unreachable")

                # A tap proposes; it must not spend a guess on its own.
                wrongC = "COUNTRIES.find(c=>c.names.en!==targets[0].country.en)"
                pt = page.evaluate(f"() => {{ const c = {wrongC}; return {{lat:c.lat,lng:c.lng}}; }}")
                page.evaluate(TAP_JS, pt); page.wait_for_timeout(250)
                check(page.locator("#pendingGuess").is_hidden() is False,
                      "a tap proposes a country")
                check(page.evaluate("state.guesses[0].length") == 0,
                      "a tap alone does not spend a guess")
                check(page.locator("#pendingName").inner_text() != "",
                      "the proposed country is named")
                page.click("#confirmGuess"); page.wait_for_timeout(250)
                check(page.locator(".hist-row").count() == 1, "guess recorded in history")
                # Each guess says what it already got right, so a try narrows
                # the search instead of just reporting a number.
                tags = page.locator(".hist-row .hist-tag")
                check(tags.count() == 2, "guess shows continent and region verdicts",
                      str(tags.count()))
                txt = page.locator(".hist-row").first.inner_text()
                check("✓" in txt or "✗" in txt, "verdicts render a tick or cross", txt)
                check("km" in page.locator("#mapReadout").inner_text(), "distance readout")
                check(page.evaluate("document.querySelectorAll('#pinLayer circle').length") >= 1, "map pin drawn")
                check(page.locator(".clue.unlocked").count() >= 1, "a clue unlocked")

                # Solve by naming the right country.
                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(250)
                check(not page.locator("#roundResult").is_hidden(), "round result shown")
                check(page.locator("#roundResult").inner_text().find(
                          page.evaluate("targets[0].names.en")) >= 0,
                      "the site name is revealed as the payoff")
                country_score = page.evaluate("totalScore()")
                check(country_score > 0, "score awarded for the country", str(country_score))

                check(abs(page.evaluate("state.roundScore[0]")
                          - page.evaluate("ROUND_PLAN[0].points")) < 0.01,
                      "naming the country takes the round's full points",
                      str(page.evaluate("state.roundScore[0]")))

                page.click("#nextBtn"); page.wait_for_timeout(300)
                # Round 2 is played through the UI rather than by calling
                # submitGuess. The input was left disabled when round 1 ended,
                # so rounds 2 and 3 were unplayable — and driving the game
                # through submitGuess hid that completely.
                check(page.locator("#pendingGuess").is_hidden(),
                      "the previous round's proposal is cleared")
                tap_country(page, "COUNTRIES.find(c=>c.names.en!==targets[1].country.en)")
                check(page.locator(".hist-row").count() >= 1,
                      "round 2 accepts a guess by tapping the map")

                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(200)
                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(200)
                # The bonus round: a single guess, then straight to the result.
                check(page.evaluate("guessesAllowed(3)") == 1,
                      "the bonus round allows one guess")
                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(250)
                check(page.evaluate("state.roundStatus[3]") == "solved",
                      "the bonus round resolves on its single guess")
                page.click("#nextBtn"); page.wait_for_timeout(300)
                check(not page.locator("#finalResult").is_hidden(), "final screen reached")
                # Round 1 took a wrong guess before the right one, which still
                # scores full marks; rounds 2-4 were solved outright. Only the
                # wrong guesses in round 2 cost anything, and they cost nothing
                # because the round was then solved.
                final = page.evaluate("totalScore()")
                mx = page.evaluate("MAX_SCORE")
                check(0 < final <= mx, f"total score stays within {mx}", str(final))

                # Info modal.
                page.locator("#learnLinks .btn").first.click(); page.wait_for_timeout(200)
                check(page.locator("#modalBackdrop").get_attribute("class").find("open") >= 0, "info modal opens")
                check(page.locator("#modalTitle").inner_text() != "", "modal has a title")
                page.click("#modalClose"); page.wait_for_timeout(150)

                check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
                ctx.close()

            # ---- mobile ----------------------------------------------------
            # Regression guards for defects found at real phone widths: sub-44px
            # tap targets and horizontal overflow. The iOS auto-zoom and
            # keyboard guards are gone with the text input -- guessing is done
            # by tapping the map, so no keyboard ever opens.
            print("\n== mobile ==")
            metrics_js = """
              () => {
                const px = el => parseFloat(getComputedStyle(el).fontSize);
                const h = sel => Math.round(document.querySelector(sel).getBoundingClientRect().height);
                return {
                  over: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                  langH: h('.lang-btn'),
                  mapH: h('.map-wrap'),
                  zoomH: h('.map-zoom button'),
                };
              }
            """
            for label, w, h in [("iPhone SE", 320, 568), ("Android", 360, 740),
                                ("iPhone 14", 390, 844), ("iPhone Max", 430, 932),
                                ("landscape", 740, 360)]:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          has_touch=True, is_mobile=True)
                page = ctx.new_page()
                page.goto(base); page.wait_for_timeout(600)
                # Field Notes covers the board on a first visit; each context is fresh.
                page.evaluate("document.getElementById('fnClose')?.click()")
                page.wait_for_timeout(120)
                m = page.evaluate(metrics_js)
                check(m["over"] <= 0, f"{label}: no horizontal overflow", f"{m['over']}px")
                check(m["langH"] >= 44, f"{label}: language button >=44px", str(m["langH"]))
                # The map is the input now, so it has to be big enough to point
                # at a small country without fighting it.
                check(m["mapH"] >= 140, f"{label}: map is tappable ({m['mapH']}px)",
                      str(m["mapH"]))
                check(m["zoomH"] >= 34, f"{label}: zoom buttons >=34px", str(m["zoomH"]))
                ctx.close()

            # The block that checked the suggestion list stayed clear of the
            # on-screen keyboard is gone: there is no text input to focus, so
            # no keyboard, and nothing for it to cover.

            # ---- photo viewer ----------------------------------------------
            # The board crops to 16:10, so the full frame has to be reachable
            # for a player to read detail off the photo. Checked on a phone,
            # since that's where the crop hurts most.
            print("\n== photo viewer ==")
            ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                      has_touch=True, is_mobile=True)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base); page.wait_for_timeout(800)
            # Field Notes covers the board on a first visit; each context is fresh.
            page.evaluate("document.getElementById('fnClose')?.click()")
            page.wait_for_timeout(120)
            # Not every entry has a photo, and the day's pick is deterministic —
            # steer round 1 onto one that does so the check isn't luck-dependent.
            page.evaluate("targets[0] = POOL.find(d => d.image); render();")
            page.wait_for_timeout(600)
            zoomable = page.evaluate("document.getElementById('photoBox').classList.contains('zoomable')")
            check(zoomable, "photo box is zoomable once the image loads")
            page.tap("#photoBox"); page.wait_for_timeout(300)
            check(page.locator("#lightbox").get_attribute("class").find("open") >= 0,
                  "tapping the photo opens the full frame")
            # Licence and attribution must travel with the enlarged photo too.
            check(page.locator("#lightboxCredit").inner_text().strip() != "",
                  "viewer carries the credit + licence line")
            check(page.evaluate("document.getElementById('lightboxImg').src") ==
                  page.evaluate("document.getElementById('siteImg').src"),
                  "viewer shows the same photo")
            page.tap("#lightboxClose"); page.wait_for_timeout(300)
            check(page.locator("#lightbox").get_attribute("class").find("open") < 0,
                  "viewer closes")
            check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
            ctx.close()

            # file:// -- fetch() is blocked, so the built-in demo set must take over.
            print("\n== file:// fallback ==")
            ctx = browser.new_context(); page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("file://" + os.path.join(ROOT, "heritle.html")); page.wait_for_timeout(700)
            # Field Notes covers the board on a first visit; each context is fresh.
            page.evaluate("document.getElementById('fnClose')?.click()")
            page.wait_for_timeout(120)
            pool = page.evaluate("POOL.length")
            check(pool == 12, "falls back to the 12-entry demo set", f"POOL={pool}")
            check(page.evaluate("targets.length") == 4, "still picks four targets")
            check(page.locator(".clue").count() == 5, "game still renders")
            check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
            ctx.close()
            browser.close()

    shutil.rmtree(ROOT, ignore_errors=True)
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
