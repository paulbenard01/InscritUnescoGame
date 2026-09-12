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
import re
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
    """The bonus round's terms have to be visible before a guess is spent."""
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

    # The day is injected before the page runs, not passed in the URL: ?day=N
    # only replays a day this browser has already finished, which is the point
    # of the change being tested.
    if day is not None:
        page.add_init_script(f"window.HERITLE_TEST_DAY = {day};")
    page.goto(base)
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
    # The mark: top-left, a real tap target, and it spins when tapped.
    mark = page.locator("#mark")
    check(mark.count() == 1, f"{label}: the mark is on the page")
    mb = mark.bounding_box()
    if mb is None:
        # Hidden because nothing was uploaded; the graceful-absence check below
        # is the one that matters.
        mb = {"width": 44, "height": 44, "x": 0, "y": 0}
    check(bool(mb) and mb["width"] >= 40 and mb["height"] >= 40,
          f"{label}: the mark is big enough to tap",
          str(mb and (round(mb["width"]), round(mb["height"]))))
    # Measured against the rendered TEXT of the wordmark, not its element box:
    # the box spans the full width of the header, so comparing boxes compares
    # two rectangles that both start at x=0 and proves nothing.
    gap = page.evaluate("""
      () => {
        const m0 = document.getElementById('mark');
        if(m0.hidden) return null;
        const wm = document.querySelector('.wordmark');
        const r = document.createRange();
        r.selectNodeContents(wm);
        const text = r.getBoundingClientRect();
        const m = document.getElementById('mark').getBoundingClientRect();
        return { markRight: m.right, textLeft: text.left, markLeft: m.left,
                 markTop: m.top };
      }
    """)
    if gap:
        check(gap["markRight"] <= gap["textLeft"] + 1,
              f"{label}: the mark sits clear to the left of the wordmark",
              f"mark ends {round(gap['markRight'])}, text starts {round(gap['textLeft'])}")
        check(gap["markLeft"] >= 0 and gap["markTop"] >= 0,
              f"{label}: and is fully on screen", str(gap))
    # The brand file is uploaded separately, so the page has to behave whether
    # it is there or not. What must never happen is a broken-image icon: the
    # button hides itself and the header falls back to the wordmark alone.
    state = page.evaluate("""
      () => { const i = document.querySelector('#mark img');
              const m = document.getElementById('mark');
              return { w: i.naturalWidth, complete: i.complete,
                       hidden: m.hidden, src: i.getAttribute('src') }; }
    """)
    check(state["src"].startswith("assets/"),
          f"{label}: the mark is loaded from assets/", state["src"])
    if state["w"] > 0:
        check(not state["hidden"], f"{label}: a mark that loads is shown")
        # One gesture, two effects: the mark spins and the menu opens. Checked
        # together because that is what a tap does -- asserting them separately
        # meant the spin check left the menu open under the next one.
        menu = page.locator("#markMenu")
        check(menu.is_hidden(), f"{label}: the menu starts closed")
        mark.click()
        page.wait_for_timeout(80)
        check("spinning" in (mark.get_attribute("class") or ""),
              f"{label}: tapping the mark starts the spin")
        check(menu.is_visible(), f"{label}: and opens the menu")
        check(page.locator("#mark").get_attribute("aria-expanded") == "true",
              f"{label}: and says so to a screen reader")
        # It drops down rather than appearing, and the page goes quiet behind it.
        anim = page.evaluate(
            "() => getComputedStyle(document.getElementById('markMenu')).animationName")
        check(anim == "menu-drop", f"{label}: the menu animates down", str(anim))
        dim = page.locator("#markDim")
        check(dim.is_visible(), f"{label}: and the page behind it is dimmed")
        mw = menu.bounding_box()["width"]
        check(mw >= 200, f"{label}: the menu is wide enough to read", str(round(mw)))
        # It has to be able to spin again, so the class must come off at the end.
        # A tap's flick coasts down under friction rather than on a timer, which
        # takes about a second and a half from the flick speed.
        page.wait_for_timeout(2400)
        check("spinning" not in (mark.get_attribute("class") or ""),
              f"{label}: the spin clears itself so it can go again")
    else:
        # Skipped, not returned from: a brand file that has not been uploaded
        # yet must not cost the other three hundred checks in this run.
        check(state["hidden"],
              f"{label}: a missing mark hides itself rather than showing a broken image",
              str(state))
        print(f"  ---- no mark uploaded; skipped the spin checks")

        # ---- the mark is a spinning top ----
        # A quick tap opens the menu; holding winds him up, and letting go
        # leaves him coasting to a stop wherever his momentum takes him. So a
        # long press must NOT also toggle the menu: the hand asked for a spin.
        angle_of = """() => {
          const m = getComputedStyle(document.querySelector('#mark img')).transform;
          const n = m && m.match(/matrix\\(([^)]+)\\)/);
          if(!n) return 0;
          const [a, b] = n[1].split(',').map(Number);
          return Math.atan2(b, a) * 180 / Math.PI;
        }"""
        if menu.is_visible():
            page.keyboard.press("Escape"); page.wait_for_timeout(120)
        mb2 = mark.bounding_box()
        cx, cy = mb2["x"] + mb2["width"] / 2, mb2["y"] + mb2["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.wait_for_timeout(900)
        spun_a = page.evaluate(angle_of)
        page.wait_for_timeout(150)
        spun_b = page.evaluate(angle_of)
        check(abs(spun_a - spun_b) > 0.5,
              f"{label}: holding the mark winds him up",
              f"{spun_a:.1f} then {spun_b:.1f}")
        page.mouse.up()
        check(menu.is_hidden(),
              f"{label}: and a long press is a spin, not a request for the menu")
        # Momentum: still turning after the finger is off.
        after_a = page.evaluate(angle_of)
        page.wait_for_timeout(200)
        after_b = page.evaluate(angle_of)
        check(abs(after_a - after_b) > 0.5,
              f"{label}: he keeps turning once released",
              f"{after_a:.1f} then {after_b:.1f}")
        # And he stops, holding whatever angle he stopped on.
        for _ in range(24):
            page.wait_for_timeout(250)
            if page.evaluate(
                "() => !document.getElementById('mark').classList.contains('spinning')"):
                break
        check(page.evaluate(
                "() => !document.getElementById('mark').classList.contains('spinning')"),
              f"{label}: and comes to rest")
        rest_a = page.evaluate(angle_of)
        page.wait_for_timeout(350)
        check(abs(page.evaluate(angle_of) - rest_a) < 0.01,
              f"{label}: resting wherever his momentum left him",
              f"{rest_a:.1f} deg")

    # The links live in that menu. The foot of the page was the wrong home for
    # them: nobody scrolls past the puzzle to find out what the game is.
    menu = page.locator("#markMenu")
    if menu.is_hidden():
        mark.click(); page.wait_for_timeout(120)
    hrefs = menu.locator("a").evaluate_all("els => els.map(e => e.href)")
    filled = page.evaluate("Object.values(LINKS).filter(Boolean).length")
    check(len(hrefs) == filled,
          f"{label}: every link with an address is shown, and only those",
          f"{len(hrefs)} shown, {filled} configured")
    check(all(h.startswith("https://") or h.startswith("mailto:") for h in hrefs),
          f"{label}: and each goes somewhere real", str(hrefs))

    # Who am I: the one item in the menu that stays inside the game.
    check(page.locator("#aboutOpen").count() == 1, f"{label}: the menu offers Who am I")
    page.locator("#aboutOpen").click(); page.wait_for_timeout(250)
    about = page.locator("#aboutBackdrop")
    check("open" in (about.get_attribute("class") or ""),
          f"{label}: which opens a panel")
    paras = page.locator("#aboutBody p").all_inner_texts()
    check(len(paras) >= 3, f"{label}: with something actually written in it",
          str(len(paras)))
    check(all(len(x) > 120 for x in paras),
          f"{label}: and none of it is a placeholder",
          str([len(x) for x in paras]))
    check(page.locator("#aboutTitle").inner_text().strip() != "",
          f"{label}: and a title")
    page.keyboard.press("Escape"); page.wait_for_timeout(200)
    check("open" not in (about.get_attribute("class") or ""),
          f"{label}: Escape closes the panel")
    # Every language has to carry the text; a missing one would silently read
    # in English, which is the failure this project keeps guarding against.
    said = {}
    for code in ("en", "fr", "es"):
        page.click(f".lang-btn[data-lang='{code}']")
        page.wait_for_timeout(120)
        if menu.is_hidden():
            mark.click(); page.wait_for_timeout(120)
        page.locator("#aboutOpen").click(); page.wait_for_timeout(180)
        said[code] = "\n".join(page.locator("#aboutBody p").all_inner_texts())
        check(len(said[code]) > 600, f"{label}: {code.upper()} Who am I is written",
              str(len(said[code])))
        page.keyboard.press("Escape"); page.wait_for_timeout(150)
    check(len({said["en"], said["fr"], said["es"]}) == 3,
          f"{label}: and each language is its own text, not a fallback")
    page.click(".lang-btn[data-lang='en']"); page.wait_for_timeout(120)
    if menu.is_hidden():
        mark.click(); page.wait_for_timeout(120)

    # It has to be dismissable, or it sits over the board.
    page.keyboard.press("Escape"); page.wait_for_timeout(120)
    check(menu.is_hidden(), f"{label}: Escape closes the menu")
    mark.click(); page.wait_for_timeout(120)
    # A real click, at a point the page itself confirms is empty. Three
    # hand-picked targets were wrong in three different ways: the photograph
    # opens the lightbox, whose overlay then swallows every later click; the
    # tagline sits underneath the open menu; and a point just right of the menu
    # landed on the nav and switched views, which broke every check after it.
    # So: ask the document what is at a candidate point, and only click where
    # nothing interactive lives.
    spot = page.evaluate("""
      () => {
        const m = document.getElementById('markMenu').getBoundingClientRect();
        const busy = 'button, a, .nav, .map-wrap, .photo-box, .modal, .lightbox,'
                   + ' input, label, .mark, .mark-menu';
        for(let y = Math.round(m.bottom) + 8; y < innerHeight - 8; y += 6){
          for(let x = 8; x < innerWidth - 8; x += 10){
            if(x > m.left - 4 && x < m.right + 4 && y > m.top - 4 && y < m.bottom + 4) continue;
            const el = document.elementFromPoint(x, y);
            if(!el || el.closest(busy)) continue;
            return { x, y, at: el.tagName.toLowerCase() + '.' + (el.className || '') };
          }
        }
        return null;
      }
    """)
    check(spot is not None, f"{label}: there is somewhere empty to tap")
    if spot:
        page.mouse.click(spot["x"], spot["y"])
        page.wait_for_timeout(150)
        check(menu.is_hidden(),
              f"{label}: and a tap anywhere else closes it", str(spot))
        check(page.locator("#markDim").is_hidden(),
              f"{label}: and the dimmer goes with it")

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
        # The page loads its mark from assets/, so the hermetic root needs them
        # -- otherwise even the fallback 404s and the check cannot tell a
        # missing file from a broken one.
        assets = os.path.join(REPO, "assets")
        if os.path.isdir(assets):
            shutil.copytree(assets, os.path.join(root, "assets"))
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
        # The photographs come first: a card is a drawer of plates, not a
        # caption. And the credits are owed in full here -- the answer is known.
        check(not page.locator("#gal").is_hidden(), "the entry opens on its photographs")
        check(bool(page.evaluate("document.getElementById('galImg').src")),
              "the gallery has a photograph in it")
        check(page.locator("#galCredit").inner_text().strip() != "",
              "the photograph is credited where nothing is left to spoil")
        links = page.locator("#modalLinks .learn-link").count()
        check(links >= 1, "the card's entry offers somewhere to learn more",
              str(links))
        hrefs = page.locator("#modalLinks .learn-link").evaluate_all(
            "els => els.map(e => e.href)")
        check(all(h.startswith("https://") for h in hrefs),
              "every link resolves to a real address", str(hrefs))
        # Rows of equal width, or they read as leftovers rather than a list.
        widths = page.locator("#modalLinks .learn-link").evaluate_all(
            "els => els.map(e => Math.round(e.getBoundingClientRect().width))")
        check(len(set(widths)) == 1, "the links line up as rows of one width",
              str(widths))
        labels = page.locator("#modalLinks .ll-text").all_inner_texts()
        check(any(l.lower().startswith("discover videos about") for l in labels),
              "the video row is named after the place", str(labels))
        check(labels[-1].lower().startswith("discover videos about"),
              "and comes last, under the reading links", str(labels))
        # One place to read about it, never two. The official listing where the
        # entry has one; the encyclopaedia only where it does not, and only for
        # a title the build checked.
        rows = page.evaluate("""
          () => {
            const pick = (test) => POOL.find(test);
            const read = (e) => {
              const div = document.createElement('div');
              div.innerHTML = learnLinks(e).join('');
              return [...div.querySelectorAll('.learn-link')].map(a => a.href);
            };
            const withSite = pick(e => e.siteId);
            const withWiki = pick(e => !e.siteId && !e.officialUrl && e.wiki);
            const withNone = pick(e => !e.siteId && !e.officialUrl && !e.wiki);
            return {
              site: withSite ? read(withSite) : null,
              wiki: withWiki ? read(withWiki) : null,
              none: withNone ? read(withNone) : null
            };
          }
        """)
        if rows["site"]:
            check(any("whc.unesco.org" in h for h in rows["site"]),
                  "an inscribed site links to its official listing", str(rows["site"]))
            check(not any("wikipedia.org" in h for h in rows["site"]),
                  "and is not also sent to the encyclopaedia", str(rows["site"]))
        check(rows["wiki"] is not None,
              "the pool has entries with no official page but a checked article")
        if rows["wiki"]:
            check(any("wikipedia.org" in h for h in rows["wiki"]),
                  "an entry with no official page falls back to the article",
                  str(rows["wiki"]))
            check(len(rows["wiki"]) == 2,
                  "which is one place to read and one to watch", str(rows["wiki"]))
        if rows["none"]:
            check(len(rows["none"]) == 1 and "youtube" in rows["none"][0],
                  "an entry with neither is left with the video search alone",
                  str(rows["none"]))
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
        # Paging, staged on an entry that certainly has several photographs
        # rather than on whichever one the day happened to catalogue. Last,
        # because this entry need not be in the collection -- and on one that
        # is not, the star is rightly hidden.
        page.evaluate("""
          () => { const e = POOL.find(d => (d.photos || []).length > 1);
                  if(e) openInfoModal(e, profile.collection[e.id]); }
        """)
        page.wait_for_timeout(200)
        shots = page.evaluate("galleryShots.length")
        check(shots > 1, "an entry with several photographs opens with them all",
              str(shots))
        if shots > 1:
            first = page.evaluate("document.getElementById('galImg').src")
            page.locator("#galNext").click(); page.wait_for_timeout(150)
            check(page.evaluate("document.getElementById('galImg').src") != first,
                  "the gallery pages to the next photograph")
            check(page.locator("#galDots i.on").count() == 1,
                  "exactly one dot marks where you are")
            page.locator("#galPrev").click(); page.wait_for_timeout(150)
            check(page.evaluate("document.getElementById('galImg').src") == first,
                  "and back again")
        if shots > 1:
            # A swipe, because reaching for a small arrow is not what a hand
            # expects to do with a photograph.
            at = page.evaluate("galleryAt")
            page.evaluate("""
              () => {
                const gal = document.getElementById('gal');
                const r = gal.getBoundingClientRect();
                const y = r.top + r.height / 2;
                const touch = (type, x) => gal.dispatchEvent(new TouchEvent(type, {
                  bubbles: true,
                  changedTouches: [new Touch({ identifier: 1, target: gal,
                                               clientX: x, clientY: y })]
                }));
                touch('touchstart', r.left + r.width * 0.8);
                touch('touchend',   r.left + r.width * 0.2);
              }
            """)
            page.wait_for_timeout(150)
            check(page.evaluate("galleryAt") == at + 1,
                  "swiping left moves to the next photograph",
                  str(page.evaluate("galleryAt")))
            page.evaluate("""
              () => {
                const gal = document.getElementById('gal');
                const r = gal.getBoundingClientRect();
                const y = r.top + r.height / 2;
                const touch = (type, x) => gal.dispatchEvent(new TouchEvent(type, {
                  bubbles: true,
                  changedTouches: [new Touch({ identifier: 1, target: gal,
                                               clientX: x, clientY: y })]
                }));
                touch('touchstart', r.left + r.width * 0.2);
                touch('touchend',   r.left + r.width * 0.8);
              }
            """)
            page.wait_for_timeout(150)
            check(page.evaluate("galleryAt") == at,
                  "and swiping right goes back")
            # A mostly-vertical drag is the page scrolling, not a swipe.
            page.evaluate("""
              () => {
                const gal = document.getElementById('gal');
                const r = gal.getBoundingClientRect();
                const x = r.left + r.width / 2;
                const touch = (type, y) => gal.dispatchEvent(new TouchEvent(type, {
                  bubbles: true,
                  changedTouches: [new Touch({ identifier: 1, target: gal,
                                               clientX: x, clientY: y })]
                }));
                touch('touchstart', r.top + 10);
                touch('touchend',   r.top + 120);
              }
            """)
            page.wait_for_timeout(150)
            check(page.evaluate("galleryAt") == at,
                  "a vertical drag is a scroll, not a swipe")
        check(page.locator("#modalFav").is_hidden(),
              "an entry not in the collection offers no star to keep it by")
        page.evaluate("document.getElementById('modalClose').click()")
        page.wait_for_timeout(200)

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
        # What a player comes back to is the stamps they have, so the earned
        # ones are on the page and the rest are behind a disclosure.
        # Visible, not total: a secret nobody has earned is not listed, which
        # is the point of it being secret.
        expected_ach = page.evaluate("visibleAchievements().length")
        total_ach = page.evaluate("ACHIEVEMENTS.length")
        check(expected_ach >= 20, "there are distinctions worth chasing",
              str(expected_ach))
        check(total_ach > expected_ach,
              "and at least one of them is a secret, so it is not listed",
              f"{total_ach} defined, {expected_ach} shown")
        secret = page.evaluate("""
          () => ACHIEVEMENTS.filter(a => a.secret).map(a => a.id)
        """)
        names = page.locator("#viewPassport .ach-tile span").all_inner_texts()
        check(bool(secret), "a secret distinction exists", str(secret))
        check(not any("Dizzy" in n for n in names),
              "and it is nowhere on the page until it is earned", str(names))
        # Earn it the way a player would: hold him down until he has turned a
        # hundred times without stopping. Measured at about ten and a half
        # seconds of holding, so this is slow but it is the real path.
        page.evaluate("showView('Today')"); page.wait_for_timeout(200)
        dz = page.locator("#mark").bounding_box()
        page.mouse.move(dz["x"] + dz["width"] / 2, dz["y"] + dz["height"] / 2)
        page.mouse.down()
        got = False
        for _ in range(40):                      # up to 20s
            page.wait_for_timeout(500)
            if page.evaluate("() => !!profile.dizzy"):
                got = True
                break
        page.mouse.up()
        check(got, "a hundred turns without stopping earns Dizzy")
        if got:
            check(page.locator(".dizzy-toast").count() == 1,
                  "and it says so when it happens rather than waiting to be found")
            check(page.evaluate("() => profile.achievements.includes('dizzy')"),
                  "and it is recorded")
            page.evaluate("showView('Passport')"); page.wait_for_timeout(300)
            shown_names = page.locator("#viewPassport .ach-tile span").all_inner_texts()
            check(any("Dizzy" in n for n in shown_names),
                  "and now it is on the page", str(shown_names[-4:]))
            head = page.locator("#viewPassport .panel-head p").last.inner_text()
            check(str(total_ach) in head,
                  "and the count includes it once it exists", head)
            # It is no longer secret to this player, so every count below is
            # against the list as it now stands.
            expected_ach = page.evaluate("visibleAchievements().length")
            check(expected_ach == total_ach,
                  "and nothing is left hidden from someone who has them all",
                  f"{expected_ach} shown, {total_ach} defined")
        # Earned enough to be worth showing, rather than whatever four entries
        # in one day happens to unlock -- otherwise "only the earned ones are
        # shown" passes against an empty list and proves nothing.
        page.evaluate("""
          () => {
            POOL.slice(0, 60).forEach(e => catalogue(e, true));
            profile.days[dayIndex] = { score: MAX_SCORE,
              statuses: ['solved','solved','solved','solved'] };
            checkAchievements();
            renderPassport();
          }
        """)
        page.wait_for_timeout(250)
        earned = page.evaluate("profile.achievements.length")
        check(earned >= 5, "a filled collection earns a spread of distinctions",
              str(earned))
        shown = page.locator("#viewPassport > .ach-grid > .ach-tile").count()
        check(shown == earned, "only the earned distinctions are on the page",
              f"{shown} shown, {earned} earned")
        head = page.locator("#viewPassport .panel-head p").last.inner_text()
        check(str(earned) in head and str(expected_ach) in head,
              "the heading counts what is earned against what exists", head)
        # An earned stamp is turned; a locked one sits straight.
        rot = page.evaluate("""
          () => {
            const e = document.querySelector('#viewPassport > .ach-grid .stamp-mark');
            const l = document.querySelector('.ach-locked .stamp-mark');
            return [getComputedStyle(e).transform, l ? getComputedStyle(l).transform : 'none'];
          }
        """)
        check(rot[0] != "none" and rot[0] != rot[1],
              "an earned stamp is struck at an angle, a locked one is not", str(rot))
        # Three to a row, and the description only when asked for.
        cols = page.evaluate("""
          () => {
            const g = document.querySelector('#viewPassport > .ach-grid');
            return getComputedStyle(g).gridTemplateColumns.split(' ').length;
          }
        """)
        check(cols == 3, "the stamps sit three to a row", str(cols))
        check(page.locator("#viewPassport .ach-detail").count() == 0,
              "no description is shown until a stamp is tapped")
        tiles = page.locator("#viewPassport > .ach-grid > .ach-tile")
        # The fourth tile: its detail has to land at the end of the second row,
        # not at the bottom of the grid, or it reads as unrelated to the tap.
        target = min(3, tiles.count() - 1)
        tiles.nth(target).click(); page.wait_for_timeout(200)
        det = page.locator("#viewPassport .ach-detail")
        check(det.count() == 1, "tapping a stamp shows its description")
        check(det.first.inner_text().strip() != "",
              "which says what it is for", det.first.inner_text())
        near = page.evaluate("""
          i => {
            const g = document.querySelector('#viewPassport > .ach-grid');
            const kids = [...g.children];
            const tile = kids.filter(k => k.matches('.ach-tile'))[i];
            const det = g.querySelector('.ach-detail');
            return det.getBoundingClientRect().top - tile.getBoundingClientRect().bottom;
          }
        """, target)
        check(0 <= near < 120,
              "and it opens just under the row that was tapped", str(near))
        tiles.nth(target).click(); page.wait_for_timeout(200)
        check(page.locator("#viewPassport .ach-detail").count() == 0,
              "tapping it again closes it")
        det = page.locator("#viewPassport .ach-locked")
        check(det.count() == 1, "the rest are behind a disclosure")
        check(not page.evaluate(
                "document.querySelector('.ach-locked').hasAttribute('open')"),
              "which starts closed")
        # Closed means out of the way, not merely unstyled.
        check(page.locator(".ach-locked .ach-tile").first.is_hidden(),
              "a locked distinction is not visible until asked for")
        page.evaluate("document.querySelector('.ach-locked summary').click()")
        page.wait_for_timeout(200)
        check(page.locator(".ach-locked .ach-tile").first.is_visible(),
              "and is there when it is")
        check(page.locator("#viewPassport .ach-tile").count() == expected_ach,
              "every distinction is accounted for, open", str(expected_ach))
        # The stamp is drawn, not set in type: a glyph in a circle sat off
        # centre at every size, which is what made it look cheap.
        box = page.locator("#viewPassport .ach-tile .stamp-mark").first.bounding_box()
        check(box and abs(box["width"] - box["height"]) < 2,
              "the stamp is round", str(box))
        centred = page.evaluate("""
          () => {
            const svg = document.querySelector('.ach-tile .stamp-mark');
            const ring = svg.querySelector('.ring.inner').getBoundingClientRect();
            const txt = svg.querySelector('.stamp-text').getBoundingClientRect();
            return [Math.abs((ring.left + ring.right) / 2 - (txt.left + txt.right) / 2),
                    Math.abs((ring.top + ring.bottom) / 2 - (txt.top + txt.bottom) / 2)];
          }
        """)
        check(max(centred) < 2.5, "and its mark sits in the middle of it",
              str(centred))
        # Names, not numbers: a ladder called Archivist I..VI is a table row.
        names = page.locator("#viewPassport .ach-tile span").all_inner_texts()
        check(not any(re.search(r"\b(I{1,3}|IV|V|VI)$", n) for n in names),
              "no distinction is named by a numeral", str(names[:8]))
        check(any("Discovering" in n or "couverte" in n or "Descubriendo" in n
                  for n in names),
              "a continent can be discovered", str(names[:8]))
        # ---- the back catalogue is not a URL any more ----
        # ?day=N used to pin any puzzle, and the archive linked to it for each
        # of the last sixty days, so the whole history could be walked by
        # counting upwards. It is honoured only for a day this browser has
        # actually finished.
        print("\n== the back catalogue ==")
        today = page.evaluate("todayIndex")
        unplayed = today - 5
        fresh = ctx.new_page()
        fresh.goto(f"{base}?day={unplayed}")
        fresh.wait_for_function("typeof POOL !== 'undefined' && POOL.length > 0", timeout=15000)
        landed = fresh.evaluate("dayIndex")
        check(landed == today,
              "asking for an unplayed day by URL lands on today instead",
              f"asked {unplayed}, got {landed}")
        # A PAST day this browser has finished is still replayable -- it spoils
        # nothing, and the archive offers it. Seeded, because the only day this
        # run has finished is today, and replaying today is not a replay: the
        # first version of this check asked for today back and then wondered why
        # it was not practice.
        # One day back, not three: the game launched on day 0, so on day 1 a
        # "three days ago" is day -2 and does not exist. Skipped entirely on
        # launch day, when there is no past day to replay at all.
        seeded = today - 1
        if seeded < 0:
            print("  ---- day 0: no past day exists yet, skipped the replay check")
        page.evaluate("""
          d => { profile.days[d] = { score: 42, at: Date.now(),
                                     statuses: ['solved','failed','solved','failed'] };
                 saveProfile(); }
        """, seeded)
        if seeded >= 0:
            again = ctx.new_page()
            again.goto(f"{base}?day={seeded}")
            again.wait_for_function("typeof POOL !== 'undefined' && POOL.length > 0", timeout=15000)
            check(again.evaluate("dayIndex") == seeded,
                  "a past day you have finished can still be replayed",
                  f"asked {seeded}, got {again.evaluate('dayIndex')}")
            check(again.evaluate("isPractice") is True,
                  "and a replay is practice, so it cannot rewrite your record")
            again.close()
        fresh.close()

        page.evaluate("showView('Archive')"); page.wait_for_timeout(300)
        # One row per day since launch, capped at the 60 the archive shows. On
        # day one that is a single row -- an archive of days nobody could have
        # played would be padding, not history.
        # Only a played day offers a way back in; the rest say so.
        links = page.locator("#viewArchive .arch-row a").count()
        locked = page.locator("#viewArchive .arch-locked").count()
        rows = page.locator("#viewArchive .arch-row").count()
        check(links + locked == rows,
              "every archive row either opens or says it was not played",
              f"{links} links + {locked} locked vs {rows} rows")
        # Today always opens, plus one per finished day the archive actually
        # lists: it shows today back sixty days, so a played day outside that
        # window has no row and therefore no link.
        played_days = page.evaluate("Object.keys(profile.days).map(Number)")
        lo = max(0, today - 60)
        expect_links = 1 + sum(1 for d in played_days if d != today and lo <= d <= today)
        check(links == expect_links,
              "and only the played days do", f"{links} links, expected {expect_links}")
        hrefs = page.locator("#viewArchive .arch-row a").evaluate_all(
            "els => els.map(e => e.getAttribute('href'))")
        check(all(h == "." or h.startswith("?day=") for h in hrefs),
              "archive links are today or a replay", str(hrefs))

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
