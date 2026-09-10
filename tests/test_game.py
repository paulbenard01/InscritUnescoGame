"""End-to-end browser test: plays the game against a served dataset.json.

Covers the wiring the pipeline swap touches -- async load, per-tier day picks
from the full pool, trilingual clue tiles, image + credit, and the file://
fallback to the built-in demo set.
"""
import http.server, json, os, socketserver, threading, sys
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []

# Self-provision the synthetic dataset so the test runs from a clean checkout.
if not os.path.exists(os.path.join(ROOT, "data", "dataset.json")):
    os.system(f'python3 {os.path.join(ROOT, "tests", "make_synthetic_dataset.py")} '
              f'{os.path.join(ROOT, "data", "dataset.json")}')

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
        base = f"http://127.0.0.1:{port}/unescle.html"

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
                tiers = page.evaluate("targets.map(t=>t.tier)")
                check(sorted(tiers) == [1,2,3], "one target per fame tier", str(tiers))
                ids = page.evaluate("targets.map(t=>t.id)")
                check(len(set(ids)) == 3, "three distinct targets", str(ids))

                check(page.locator(".wordmark").inner_text() == "Unescle", "wordmark intact")
                check(page.locator("#modeBadge").inner_text() != "", "mode badge rendered")
                check(page.locator(".clue").count() == 5, "five clue tiles")

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

                # A wrong guess: distance readout, history row, map pin.
                wrong = page.evaluate("POOL.find(d=>d.id!==targets[0].id).names.en")
                page.fill("#guessInput", wrong[:12]); page.wait_for_timeout(200)
                check(page.locator(".suggestion").count() > 0, "autocomplete suggests")
                page.locator(".suggestion").first.click(); page.wait_for_timeout(250)
                check(page.locator(".hist-row").count() == 1, "guess recorded in history")
                check("km" in page.locator("#mapReadout").inner_text(), "distance readout")
                check(page.evaluate("document.querySelectorAll('#pinLayer circle').length") >= 1, "map pin drawn")
                check(page.locator(".clue.unlocked").count() >= 1, "a clue unlocked")

                # Solve the round outright.
                page.evaluate("submitGuess(targets[0])"); page.wait_for_timeout(250)
                check(not page.locator("#roundResult").is_hidden(), "round result shown")
                score = page.evaluate("totalScore()")
                check(score > 0, "score awarded", str(score))

                page.click("#nextBtn"); page.wait_for_timeout(200)
                page.evaluate("submitGuess(targets[1])"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(200)
                page.evaluate("submitGuess(targets[2])"); page.wait_for_timeout(200)
                page.click("#nextBtn"); page.wait_for_timeout(300)
                check(not page.locator("#finalResult").is_hidden(), "final screen reached")
                # Round 1 took a wrong guess first (0.85 x 100/3), rounds 2-3 were
                # solved first try (100/3 each) -> 95.
                final = page.evaluate("totalScore()")
                check(final == 95, "scoring matches the guess-fraction table", str(final))

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
                page.fill("#guessInput", page.evaluate("POOL[0].names.en")[:6])
                page.wait_for_timeout(900)
                check(page.evaluate(visible_js) is True,
                      f"{label} +keyboard: first suggestion on screen")
                ctx.close()

            # file:// -- fetch() is blocked, so the built-in demo set must take over.
            print("\n== file:// fallback ==")
            ctx = browser.new_context(); page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("file://" + os.path.join(ROOT, "unescle.html")); page.wait_for_timeout(700)
            pool = page.evaluate("POOL.length")
            check(pool == 12, "falls back to the 12-entry demo set", f"POOL={pool}")
            check(page.evaluate("targets.length") == 3, "still picks three targets")
            check(page.locator(".clue").count() == 5, "game still renders")
            check(not errors, "no uncaught page errors", "; ".join(errors[:3]))
            ctx.close()
            browser.close()

    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
