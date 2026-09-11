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


TAP_JS = """(pt) => {
  const p = project(pt.lat, pt.lng);
  const svg = document.getElementById('mapSvg');
  const r = svg.getBoundingClientRect();
  svg.dispatchEvent(new MouseEvent('click', {
    clientX: r.left + ((p.x - mapView.x) / mapView.w) * r.width,
    clientY: r.top  + ((p.y - mapView.y) / mapView.h) * r.height,
    bubbles: true }));
}"""


def tap_guess(page, country_js):
    """Guess by pointing at the map, the way a player does."""
    pt = page.evaluate(f"() => {{ const c = {country_js}; return {{lat:c.lat, lng:c.lng}}; }}")
    page.evaluate(TAP_JS, pt)
    page.wait_for_timeout(180)
    page.click("#confirmGuess")
    page.wait_for_timeout(220)


def T_BONUS_INTRO_SHOWN(page):
    """The one-guess rule has to be visible before the guess is spent."""
    return page.evaluate("""
      () => document.getElementById('mapReadout').textContent === t().bonusIntro
    """)


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
    check(page.evaluate("targets.length") == 4, f"{label}: four targets chosen")
    # Information hierarchy: the accent colour has to keep meaning one thing.
    # It marks what is live or actionable -- the current round, the guess
    # button, your pin. When a language setting and a category caption also
    # wore it, it meant nothing and the eye had nowhere to go first.
    gold_boxes = page.evaluate("""
      () => {
        const top = document.querySelector('.photo-box').getBoundingClientRect().top;
        const near = (c) => { const m = String(c).match(/\\d+/g); return m &&
          Math.abs(+m[0]-201)<12 && Math.abs(+m[1]-162)<12 && Math.abs(+m[2]-75)<12; };
        const out = [];
        for(const el of document.querySelectorAll('header *, .nav *, .rounds-row *, .meta-row *')){
          const b = el.getBoundingClientRect();
          if(!b.height || b.top >= top) continue;
          const cs = getComputedStyle(el);
          const boxed = (near(cs.borderTopColor) && cs.borderTopWidth !== '0px')
                     || near(cs.backgroundColor);
          if(boxed) out.push(el.className || el.tagName);
        }
        return out;
      }
    """)
    check(len(gold_boxes) <= 2,
          f"{label}: the accent colour stays scarce above the puzzle",
          f"{len(gold_boxes)}: {gold_boxes}")
    # The language control is a preference, not a move: it must not be one.
    check(not any('lang' in str(c) for c in gold_boxes),
          f"{label}: the language control does not wear the accent colour")
    # Guessing is done by pointing at the map. Every country the game will
    # accept has to be reachable that way, or it cannot be guessed at all.
    check(page.evaluate("!!LAND_SHAPES"), f"{label}: country shapes loaded")
    # Reachable means a tap can actually select it: a polygon to land in, or
    # -- for a country too small to have one, like Vatican City -- close
    # enough to its centroid for the snap to catch it.
    unreachable = page.evaluate("""
      () => COUNTRIES.filter(c => {
        if(!c.iso) return true;
        if(LAND_SHAPES[c.iso]) return false;
        // The path a real tap takes, not countryNear in isolation: the
        // polygon test runs first and can answer with the enclosing country.
        const p = project(c.lat, c.lng);
        const got = countryByIso(isoAt(p.x, p.y)) || countryNear(p.x, p.y);
        return !(got && got.id === c.id);
      }).map(c => c.names.en)
    """)
    check(not unreachable, f"{label}: every guessable country can be tapped",
          ", ".join(unreachable[:5]))
    # And no entry may be left with an answer nobody can give.
    unwinnable = page.evaluate("""
      () => { const ok = new Set(COUNTRIES.map(c => c.id));
              return POOL.filter(e => !(e.countryIds || []).some(id => ok.has(id)))
                         .map(e => e.names.en); }
    """)
    check(not unwinnable, f"{label}: no entry has an unreachable answer",
          ", ".join(unwinnable[:5]))
    # Open water is not a guess, and must not spend one.
    before = page.evaluate("state.guesses[0].length")
    page.evaluate(TAP_JS, {"lat": 0, "lng": -140})
    page.wait_for_timeout(200)
    check(page.evaluate("state.guesses[0].length") == before,
          f"{label}: tapping open water does not spend a guess")
    check(page.locator("#pendingGuess").is_hidden(),
          f"{label}: open water proposes nothing")
    # The confirm bar sits below the map; on a phone that is off-screen, so a
    # pin would appear with no visible way to commit it.
    pt = page.evaluate("() => { const c = COUNTRIES[0]; return {lat:c.lat, lng:c.lng}; }")
    page.evaluate(TAP_JS, pt)
    page.wait_for_timeout(700)
    pb, vh = page.locator("#pendingGuess").bounding_box(), page.viewport_size["height"]
    # Fully visible is the requirement. The scroll-margin cushion only applies
    # when a scroll actually happens, and once the header stopped wasting 60px
    # the bar fits without one -- the browser then leaves it where it is, a
    # couple of pixels off the bottom, which is in view and tappable.
    check(bool(pb) and pb["y"] >= 0 and pb["y"] + pb["height"] <= vh,
          f"{label}: the confirm bar is fully on screen",
          f"bottom={pb and round(pb['y'] + pb['height'])} vh={vh}")
    page.evaluate("clearPending(); renderMapForRound();")
    page.wait_for_timeout(150)
    # The detailed geometry is fetched, not inlined, so a missing or
    # canvas-mismatched file degrades silently to the coarse outline.
    check(page.evaluate("LAND_PATH !== null"), f"{label}: detailed map geometry loaded")
    # Pin radii are in world units and must counter-scale, or a guess dot
    # covers a whole country once you zoom in.
    r_world, r_zoom = page.evaluate("""
      () => {
        addPin(0, 0, 'far', false);
        const c = document.querySelector('#pinLayer circle');
        const a = parseFloat(c.getAttribute('r'));
        mapView.w = MAP_W / 8; applyView();
        const b = parseFloat(c.getAttribute('r'));
        mapView = { x:0, y:0, w:MAP_W, h:MAP_H }; applyView();
        return [a, b];
      }
    """)
    check(r_zoom < r_world / 4, f"{label}: pins scale with the map",
          f"world r={r_world} zoomed r={r_zoom}")

    # ---- every language renders ----
    # Needles from each tagline that do not appear in the other two.
    for code, needle in (("fr", "chaque jour"), ("es", "cada día"), ("en", "each day")):
        page.click(f".lang-btn[data-lang='{code}']")
        page.wait_for_timeout(120)
        check(needle in page.locator("#tagline").inner_text().lower(),
              f"{label}: {code.upper()} renders")

    # ---- a guess must report its verdict without scrolling ----
    # The verdict used to live only in the history list below the map, so on a
    # phone a guess looked like it had done nothing.
    # Played through the map rather than by calling submitGuess: the input is
    # the part most likely to break, and driving the game past it would hide
    # exactly that.
    tap_guess(page, "COUNTRIES.find(c => c.names.en !== targets[0].country.en)")
    page.wait_for_timeout(600)      # the panel is scrolled into view smoothly
    readout = page.locator("#mapReadout")
    check("last-guess" in (readout.get_attribute("class") or ""),
          f"{label}: the guess verdict is shown under the map")
    check(readout.locator(".hist-tag").count() == 2,
          f"{label}: continent and region are marked on it",
          str(readout.locator(".hist-tag").count()))
    rbox, vh = readout.bounding_box(), page.viewport_size["height"]
    # Wholly on screen and not flush against the bottom edge.
    check(bool(rbox) and rbox["y"] >= 0 and rbox["y"] + rbox["height"] <= vh - 8,
          f"{label}: the verdict is on screen without scrolling",
          f"bottom={rbox and round(rbox['y'] + rbox['height'])} vh={vh}")

    # ---- an element inscribed by several states accepts any of them ----
    # Marking eleven of Nowruz's twelve countries wrong would be a bug, not a
    # hard round, and the pipeline used to keep only the first of them.
    multi = page.evaluate("""
      () => {
        const e = POOL.find(d => d.countryIds && d.countryIds.length > 1);
        if(!e) return null;
        return { n: e.countryIds.length, names: e.countryNames.map(c => c.en) };
      }
    """)
    check(multi is not None, f"{label}: the pool has multinational entries")
    if multi:
        ok = page.evaluate("""
          names => {
            const e = POOL.find(d => d.countryIds && d.countryIds.length > 1);
            const saved = targets[0];
            targets[0] = e;
            const before = state.round; state.round = 0;
            const accepted = names.map(n =>
              targetCountries().some(c => c.names.en === n));
            targets[0] = saved; state.round = before;
            return accepted;
          }
        """, multi["names"])
        check(all(ok), f"{label}: every inscribing country counts as correct",
              str(list(zip(multi["names"], ok))))

    # ---- a wrong guess reveals another photograph, and you can page back ----
    # A single weak photo made a round unguessable rather than hard, so each
    # miss uncovers another. Driven through submitGuess rather than by poking
    # the state, because the view advancing is part of the behaviour.
    photo_state = page.evaluate("""
      () => {
        const tg = POOL.find(t => (t.photos || []).length > 2);
        if(!tg) return null;
        targets[state.round] = tg;
        // Staging a new target for this round means staging a live round: the
        // guess above can land on a second inscribing country and solve it,
        // and submitGuess refuses to act on a finished round.
        state.roundStatus[state.round] = null;
        photoView[state.round] = 0;
        render();
        const first = currentPhoto(tg).file;
        const unlockedBefore = unlockedCount(tg);
        // Not already guessed: a repeat is refused outright, and the first
        // non-answer country is exactly the one the verdict check above used.
        const wrong = COUNTRIES.find(c => !targetCountries().some(a => a.id === c.id)
                                       && !currentGuesses().some(g => g.id === c.id));
        submitGuess(wrong);
        const after = currentPhoto(tg).file;
        const shownIdx = photoIndexFor(tg);
        const unlockedAfter = unlockedCount(tg);
        stepPhoto(-1);
        const backIdx = photoIndexFor(tg), backOne = currentPhoto(tg).file;
        stepPhoto(1);
        const forwardAgain = currentPhoto(tg).file;
        return { first, after, backOne, forwardAgain, shownIdx, backIdx,
                 unlockedBefore, unlockedAfter,
                 credit: document.getElementById('photoCredit').textContent };
      }
    """)
    check(photo_state is not None, f"{label}: some entries carry several photos")
    if photo_state:
        check(photo_state["unlockedAfter"] == photo_state["unlockedBefore"] + 1,
              f"{label}: a guess unlocks one more photograph",
              f"{photo_state['unlockedBefore']} -> {photo_state['unlockedAfter']}")
        check(photo_state["shownIdx"] == photo_state["unlockedAfter"] - 1,
              f"{label}: the newly unlocked photograph is the one shown",
              f"index {photo_state['shownIdx']} of {photo_state['unlockedAfter']}")
        # Back one from whatever is showing -- not back to the first, since a
        # guess earlier in the round may already have unlocked others.
        check(photo_state["backIdx"] == photo_state["shownIdx"] - 1
              and photo_state["backOne"] != photo_state["after"],
              f"{label}: you can page back to an earlier photograph",
              f"index {photo_state['backIdx']} after {photo_state['shownIdx']}")
        check(photo_state["forwardAgain"] == photo_state["after"],
              f"{label}: and forward again to the newest")
        # A photographer's name or licence template often names a country.
        check(photo_state["credit"] == "",
              f"{label}: no photo credit while the round is live",
              photo_state["credit"])
    page.reload()
    page.wait_for_function("typeof POOL !== 'undefined' && POOL.length > 0", timeout=15000)
    page.evaluate("document.getElementById('fnClose')?.click()")
    page.wait_for_timeout(250)

    # ---- round 1: win outright ----
    page.evaluate("submitGuess(targetCountry())")
    page.wait_for_timeout(250)
    check(page.evaluate("state.roundStatus[0]") == "solved", f"{label}: round 1 won")
    check(abs(page.evaluate("state.roundScore[0]") - page.evaluate("ROUND_PLAN[0].points")) < 0.01,
          f"{label}: naming the country takes full marks",
          str(page.evaluate("state.roundScore[0]")))
    page.click("#nextBtn"); page.wait_for_timeout(250)

    # ---- round 2: three wrong guesses -- the last one is what scores ----
    allowed = page.evaluate("guessesAllowed(1)")
    check(allowed == 3, f"{label}: three guesses on a heritage round", str(allowed))
    wrongs = page.evaluate("""
      () => COUNTRIES.filter(c => c.names.en !== targets[1].country.en)
                     .slice(0, 3).map(c => c.id)
    """)
    check(len(wrongs) == 3, f"{label}: three wrong countries available", str(len(wrongs)))
    for cid in wrongs:
        page.evaluate("id => submitGuess(COUNTRIES.find(c => c.id === id))", cid)
        page.wait_for_timeout(60)
    check(page.evaluate("state.roundStatus[1]") == "failed", f"{label}: round 2 missed",
          str(page.evaluate("state.roundStatus[1]")))
    check(page.evaluate("state.guesses[1].length") == 3,
          f"{label}: the round ends after its allowance")
    # The last guess scores by proximity, so a miss is worth something but
    # never the full round.
    expected = page.evaluate("""
      () => { const g = state.guesses[1][state.guesses[1].length - 1];
              return ROUND_PLAN[1].points * proximity(g.km, false); }
    """)
    check(abs(page.evaluate("state.roundScore[1]") - expected) < 0.01,
          f"{label}: the last guess is the one that scores",
          f"{page.evaluate('state.roundScore[1]')} vs {expected}")
    check(page.evaluate("state.roundScore[1]") < page.evaluate("ROUND_PLAN[1].points"),
          f"{label}: a missed round scores less than full marks")
    page.click("#nextBtn"); page.wait_for_timeout(250)

    # ---- round 3: a wrong guess first, then solve -- still full marks ----
    w = page.evaluate("() => COUNTRIES.find(c => c.names.en !== targets[2].country.en).id")
    page.evaluate("id => submitGuess(COUNTRIES.find(c => c.id === id))", w)
    page.wait_for_timeout(120)
    page.evaluate("submitGuess(targetCountry())")
    page.wait_for_timeout(250)
    check(page.evaluate("state.roundStatus[2]") == "solved", f"{label}: round 3 solved")
    check(abs(page.evaluate("state.roundScore[2]") - page.evaluate("ROUND_PLAN[2].points")) < 0.01,
          f"{label}: solving late still takes full marks")
    page.click("#nextBtn"); page.wait_for_timeout(250)

    # ---- round 4: the intangible bonus round ----
    # If the pool has no traditions in it the bonus round silently falls back
    # to a fourth site, so check the pool first -- otherwise the failure reads
    # as a game bug when it is a dataset that predates the intangible pool.
    check(page.evaluate("POOL.some(d => d.type === 'immaterial')"),
          f"{label}: the dataset carries intangible entries")
    check(page.evaluate("targets[3].type") == "immaterial",
          f"{label}: the bonus round is an intangible element",
          str(page.evaluate("targets[3].type")))
    # A tradition is far harder to place than a building, so a single guess
    # meant the round was lost by default rather than played. Three, like the
    # rest -- and the photograph ladder that comes with them.
    check(page.evaluate("guessesAllowed(3)") == 3,
          f"{label}: the bonus round allows three guesses",
          str(page.evaluate("guessesAllowed(3)")))
    check(T_BONUS_INTRO_SHOWN(page), f"{label}: the bonus round says so before the guess")
    wrongs = page.evaluate("""
      () => COUNTRIES.filter(c => !targetCountries().some(a => a.id === c.id))
                     .slice(0, 3).map(c => c.id)
    """)
    check(len(wrongs) == 3, f"{label}: three wrong countries available for the bonus")
    for i, cid in enumerate(wrongs):
        page.evaluate("id => submitGuess(COUNTRIES.find(c => c.id === id))", cid)
        page.wait_for_timeout(80)
        if i == 0:
            check(page.evaluate("state.roundStatus[3]") is None,
                  f"{label}: one wrong guess does not end the bonus round",
                  str(page.evaluate("state.roundStatus[3]")))
    check(page.evaluate("state.roundStatus[3]") == "failed", f"{label}: bonus round resolved")
    check(page.evaluate("state.guesses[3].length") == 3,
          f"{label}: the bonus round ends after its allowance",
          str(page.evaluate("state.guesses[3].length")))
    page.click("#nextBtn"); page.wait_for_timeout(400)

    # ---- final screen ----
    check(not page.locator("#finalResult").is_hidden(), f"{label}: final screen shown")
    # The badge row is emptied on this screen; an empty bordered pill used to
    # draw a small box above the summary.
    check(page.locator(".meta-row").is_hidden() or
          (page.locator(".meta-row").bounding_box() or {}).get("height", 0) == 0,
          f"{label}: no empty badge box above the summary")
    grid = page.locator("#finalResult .share-grid").inner_text()
    check("—" in grid or len(grid.strip()) > 0, f"{label}: share grid rendered", repr(grid))
    check(grid.count("\n") == 3, f"{label}: share grid has one line per round", repr(grid))
    score = page.evaluate("totalScore()")
    mx = page.evaluate("MAX_SCORE")
    check(mx == 150, f"{label}: the day is scored out of 150", str(mx))
    check(0 < score < mx, f"{label}: score reflects the missed rounds", f"{score}/{mx}")
    statuses = page.evaluate("state.roundStatus")
    check(statuses == ["solved", "failed", "solved", "failed"],
          f"{label}: 2 solved / 2 missed", str(statuses))

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
            # The map geometry is real either way -- it is independent of the
            # dataset, and without it the run reports a missing-file failure
            # that says nothing about the game.
            land = os.path.join(REPO, "data", "land.json")
            if os.path.exists(land):
                shutil.copy(land, os.path.join(root, "data", "land.json"))
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
        # A card is the only way back to something met in a round, so it has to
        # open, and open with links that go somewhere.
        page.locator("#viewCollection .card").first.click()
        page.wait_for_timeout(300)
        check("open" in (page.locator("#modalBackdrop").get_attribute("class") or ""),
              "a collection card opens its entry")
        links = page.locator("#modalLinks .learn-link").count()
        check(links >= 1, "the card's entry offers somewhere to learn more",
              str(links))
        hrefs = page.locator("#modalLinks .learn-link").evaluate_all(
            "els => els.map(e => e.href)")
        check(all(h.startswith("https://") for h in hrefs),
              "every link resolves to a real address", str(hrefs))
        # Favouriting from the card that is already open.
        page.locator("#modalFav").click(); page.wait_for_timeout(200)
        page.evaluate("document.getElementById('modalClose').click()")
        page.wait_for_timeout(250)
        check(page.evaluate("Object.keys(profile.favourites).length") == 1,
              "the star keeps an entry",
              str(page.evaluate("Object.keys(profile.favourites).length")))
        heads = page.locator("#viewCollection .panel-head h2").all_inner_texts()
        check(len(heads) == 2 and "Favourite" in heads[0],
              "favourites get a shelf of their own, first", str(heads))
        check(page.locator("#viewCollection .card .fav.on").count() == 1,
              "and the starred card shows as kept")
        # Starring must not open the card -- it is a button of its own.
        page.evaluate("document.getElementById('modalBackdrop').classList.remove('open')")
        page.locator("#viewCollection .card .fav").first.click()
        page.wait_for_timeout(250)
        check("open" not in (page.locator("#modalBackdrop").get_attribute("class") or ""),
              "tapping the star does not open the card")
        check(page.evaluate("Object.keys(profile.favourites).length") == 0,
              "and starring again puts it back")
        page.evaluate("showView('Passport')"); page.wait_for_timeout(300)
        check(page.locator("#viewPassport .stamp").count() >= 1, "passport shows a stamp")
        # Four one-off distinctions plus one Archivist tier per threshold.
        expected_ach = page.evaluate("ACHIEVEMENTS.length")
        check(page.locator("#viewPassport .ach").count() == expected_ach,
              "every distinction is listed", str(expected_ach))
        check(expected_ach >= 9, "the Archivist ladder has tiers", str(expected_ach))
        page.evaluate("showView('Archive')"); page.wait_for_timeout(300)
        # One row per day since launch, capped at the 60 the archive shows. On
        # day one that is a single row -- an archive of days nobody could have
        # played would be padding, not history.
        expected = min(page.evaluate("todayIndex") + 1, 61)
        check(page.locator("#viewArchive .arch-row").count() == expected,
              "archive lists one row per day since launch",
              f"{page.locator('#viewArchive .arch-row').count()} vs {expected}")
        # The row for a day that was actually played. Nothing looked at this
        # before, and it had been printing "76 / undefined" ever since the
        # score line started carrying its maximum.
        played = page.locator("#viewArchive .arch-row .arch-score").first.inner_text()
        check("undefined" not in played and "NaN" not in played,
              "a played day shows a real score in the archive", played)
        check("/" in played and played.strip().split("/")[-1].strip().isdigit(),
              "the archive score carries its maximum", played)
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
