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

                pool = page.evaluate("POOL.length")
                check(pool > 2000, "loaded full dataset from data/dataset.json", f"POOL={pool}")
                plan = page.evaluate("targets.map(t=>({type:t.type,tier:t.tier}))")
                check([p["type"] for p in plan] == ["material", "immaterial", "material"],
                      "two material rounds and one intangible", str(plan))
                check(sorted(p["tier"] for p in plan) == [1, 2, 3],
                      "difficulty ramps across the three rounds", str(plan))
                ids = page.evaluate("targets.map(t=>t.id)")
                check(len(set(ids)) == 3, "three distinct targets", str(ids))

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
                check("devinettes" in fr, "FR tagline", fr)
                page.click(".lang-btn[data-lang='es']"); page.wait_for_timeout(150)
                es = page.locator("#tagline").inner_text()
                check("adivinanzas" in es, "ES tagline", es)
                page.click(".lang-btn[data-lang='en']"); page.wait_for_timeout(150)

                # Guesses are countries now, never site names.
                check(page.evaluate("COUNTRIES.length") > 10, "country list loaded",
                      str(page.evaluate("COUNTRIES.length")))
                site_name = page.evaluate("targets[0].names.en")
                page.fill("#guessInput", site_name[:14]); page.wait_for_timeout(250)
                check(page.locator(".suggestion").count() == 0,
                      "typing the site name suggests nothing (names aren't the solve)")

                # A wrong country: distance readout, history row, map pin.
                wrong = page.evaluate(
                    "COUNTRIES.find(c=>c.names.en!==targets[0].country.en).names.en")
                page.fill("#guessInput", wrong[:12]); page.wait_for_timeout(250)
                check(page.locator(".suggestion").count() > 0, "autocomplete suggests countries")
                page.locator(".suggestion").first.click(); page.wait_for_timeout(250)
                check(page.locator(".hist-row").count() == 1, "guess recorded in history")
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

                # The bonus round: one pin on the map, scored by proximity.
                check(page.evaluate("state.bonusOpen") is True, "bonus round offered")
                page.evaluate("""
                  () => {
                    const t = targets[state.round];
                    const p = project(t.lat, t.lng);
                    const r = document.getElementById('mapSvg').getBoundingClientRect();
                    document.getElementById('mapSvg').dispatchEvent(new MouseEvent('click', {
                      clientX: r.left + (p.x / MAP_W) * r.width,
                      clientY: r.top  + (p.y / MAP_H) * r.height, bubbles: true }));
                  }
                """)
                page.wait_for_timeout(300)
                withBonus = page.evaluate("totalScore()")
                check(withBonus > country_score, "an accurate pin adds bonus points",
                      f"{country_score} -> {withBonus}")
                check(page.evaluate("state.bonusOpen") is False, "bonus closes after the pin")

                page.click("#nextBtn"); page.wait_for_timeout(200)
                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(200)
                page.click("#skipBonusBtn"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(200)
                page.evaluate("submitGuess(targetCountry())"); page.wait_for_timeout(200)
                page.click("#skipBonusBtn"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(300)
                check(not page.locator("#finalResult").is_hidden(), "final screen reached")
                # Country is 80% of a round, the pin bonus the other 20%. Round 1
                # took a wrong guess then an exact pin; rounds 2-3 were solved
                # first try with the bonus skipped. Total must stay within 100.
                final = page.evaluate("totalScore()")
                check(0 < final <= 100, "total score stays within 100", str(final))

                # Info modal.
                page.locator("#learnLinks .btn").first.click(); page.wait_for_timeout(200)
                check(page.locator("#modalBackdrop").get_attribute("class").find("open") >= 0, "info modal opens")
                check(page.locator("#modalTitle").inner_text() != "", "modal has a title")
                page.click("#modalClose"); page.wait_for_timeout(150)

                check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
                ctx.close()

            # ---- mobile ----------------------------------------------------
            # Regression guards for defects found at real phone widths: iOS
            # auto-zoom on a sub-16px input, sub-44px tap targets, and the
            # suggestion list rendering below the fold once the keyboard opens.
            print("\n== mobile ==")
            metrics_js = """
              () => {
                const px = el => parseFloat(getComputedStyle(el).fontSize);
                const h = sel => Math.round(document.querySelector(sel).getBoundingClientRect().height);
                return {
                  over: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                  inputFont: px(document.getElementById('guessInput')),
                  langH: h('.lang-btn'),
                  inputH: h('#guessInput'),
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
                m = page.evaluate(metrics_js)
                check(m["over"] <= 0, f"{label}: no horizontal overflow", f"{m['over']}px")
                # Under 16px, iOS Safari zooms the page in on focus and stays there.
                check(m["inputFont"] >= 16, f"{label}: input >=16px (no iOS zoom)",
                      str(m["inputFont"]))
                check(m["langH"] >= 44, f"{label}: language button >=44px", str(m["langH"]))
                check(m["inputH"] >= 44, f"{label}: guess box >=44px", str(m["inputH"]))
                ctx.close()

            # The suggestion list must stay on screen with the keyboard open.
            # The viewport is shrunk to stand in for the room a keyboard leaves.
            visible_js = """
              () => {
                const s = document.querySelector('.suggestion');
                if (!s) return null;
                const b = s.getBoundingClientRect();
                return b.top >= 0 && b.bottom <= innerHeight;
              }
            """
            for label, w, h in [("iPhone SE", 320, 308), ("Android", 360, 480),
                                ("landscape", 740, 200)]:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          has_touch=True, is_mobile=True)
                page = ctx.new_page()
                page.goto(base); page.wait_for_timeout(600)
                page.tap("#guessInput")
                page.fill("#guessInput", page.evaluate("COUNTRIES[0].names.en")[:6])
                page.wait_for_timeout(900)
                check(page.evaluate(visible_js) is True,
                      f"{label} +keyboard: first suggestion on screen")
                ctx.close()

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
            pool = page.evaluate("POOL.length")
            check(pool == 12, "falls back to the 12-entry demo set", f"POOL={pool}")
            check(page.evaluate("targets.length") == 3, "still picks three targets")
            check(page.locator(".clue").count() == 5, "game still renders")
            check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
            ctx.close()
            browser.close()

    shutil.rmtree(ROOT, ignore_errors=True)
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
