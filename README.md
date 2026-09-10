# Unescle

A trilingual (EN/FR/ES) daily heritage-guessing game. Three rounds a day drawn
from World Heritage Sites and Intangible Cultural Heritage elements, scored out
of 100, with a Globle-style world-map distance mechanic.

```
unescle.html        the whole game — single file, no build step
build_dataset.py    Wikidata + Wikimedia Commons pipeline
data/dataset.json   pipeline output (metadata; not committed until it's real)
images/{id}.jpg     one photo per entry (only the day's 3 are ever requested)
tests/              offline pipeline fixtures + a Playwright end-to-end test
```

## Status

The game shell is finished and wired to the real dataset format. **`data/dataset.json`
has not been generated yet** — see [Known gap](#known-gap-the-dataset-has-not-been-built)
below. Until it exists, the game falls back to its built-in 12-entry demo set
and plays end-to-end on that.

## Running it

Any static server works; `fetch()` needs one (over `file://` the game falls back
to the demo set by design):

```sh
python3 -m http.server 8000     # then open http://localhost:8000/unescle.html
```

## Building the dataset

```sh
pip install requests
python build_dataset.py                  # full run: ~2,100 entries + photos
python build_dataset.py --limit 40       # smoke test against the live endpoints
python build_dataset.py --skip-images    # metadata only
python build_dataset.py --fixture tests/fixtures   # offline, no network
```

Set `UNESCLE_CONTACT` to your own contact address — Wikimedia requires a
descriptive User-Agent and will throttle anonymous-looking clients.

The run prints a distribution report and writes:

- `data/dataset.json` — `{ meta, countries, entries }`
- `data/noncommercial_images.json` — entries whose only photo is CC BY-NC-\*
- `images/{id}.jpg`

Commit `data/` and `images/` to publish them; the Pages workflow copies whatever
is present and the game degrades gracefully when either is missing.

### Dataset shape

```jsonc
{
  "meta":      { "count": 2122, "withPhoto": 1840, "translationCoverage": { … } },
  "countries": { "Q29": { "en": "Spain", "fr": "Espagne", "es": "España" } },
  "entries": [{
    "id": "goreme-national-park",   // also the image filename and history key
    "qid": "Q170471",
    "type": "material",             // material | immaterial
    "tier": 1,                      // 1 widely recognized … 3 hidden gem
    "names": { "en": …, "fr": …, "es": … },
    "lat": 38.65, "lng": 34.83,
    "approx": true,                 // coordinate is the country centroid
    "continent": "AS",              // code, translated in the game's i18n table
    "category": "mixed",            // cultural | natural | mixed | site | intangible
    "country": "Q43",               // key into `countries`
    "year": 1985,
    "sitelinks": 52,
    "aliases": [ … ],
    "image": { "path": …, "license": "CC BY-SA 4.0", "credit": "A. Photographer",
               "nonCommercial": true }
  }]
}
```

Categorical fields travel as **codes**, not prose, and are translated in
`unescle.html`'s UI table. Wikidata's FR/ES label coverage is thin for
lesser-known entries, so translating these per-entry would leave most clue tiles
in English; only genuinely per-entry text (names, country names, descriptions)
is pulled from Wikidata, and every fallback to English is counted and reported.

### Design notes worth knowing

- **Pagination.** One query with six `OPTIONAL` blocks over ~1,300 items
  exceeds the 60s WDQS timeout, so each query's spine is an `ORDER BY`'d
  subquery with `LIMIT`/`OFFSET`, fetched a page at a time with backoff.
- **Cross-product rows.** Several `OPTIONAL`s together mean an item with two
  images and two criteria returns four rows. Rows are grouped by QID and merged;
  treating each row as an entry produced duplicates.
- **Stable ids.** Slugs are ASCII-folded and disambiguated with the QID rather
  than a counter, so an id never shifts between runs — ids are image filenames
  and `localStorage` keys.
- **Coordinate fallback.** Intangible elements are practices, not places, and
  mostly have no `P625`. Those fall back to the linked country's coordinate
  (one batched query) and are marked `"approx": true`.
- **Fame tiers are rank tertiles, not value cutoffs.** Sitelink counts are
  long-tailed with a large tie at the bottom — a third or more of entries sit on
  0–1 sitelinks — so a 33rd-percentile *value* cutoff lands on the minimum,
  matches nothing, and leaves tier 3 empty and the day's third round unfillable.
  Ranking within each type is immune to that. Material and intangible entries
  are tiered separately: an intangible element with 12 sitelinks is famous, a
  World Heritage Site with 12 is obscure.
- **Licensing.** Every photo keeps its Commons licence and attribution, shown in
  the `.photo-credit` line. Non-commercial-only photos are flagged
  (`image.nonCommercial`) and listed in `data/noncommercial_images.json` —
  flagged, never silently discarded, in case the site ever needs to be
  commercial-safe.

## Tests

```sh
python build_dataset.py --fixture tests/fixtures --out /tmp/ds.json   # pipeline
python tests/test_game.py                                            # end-to-end
```

`tests/fixtures/` holds recorded-shape SPARQL responses covering the cases that
actually break: missing and malformed coordinates, absent FR/ES labels,
non-ASCII names, id collisions, dual-designated items and NC licences.
`tests/test_game.py` serves the repo, plays a full three-round game in Chromium
at desktop and mobile widths, and checks the `file://` fallback path.
`tests/make_synthetic_dataset.py` generates a full-size (~2,100 entry) stand-in
dataset, tiered by the pipeline's own function.

### Mobile

The suite checks five phone viewports (320–430px plus landscape) for horizontal
overflow and tap-target size, and checks that the suggestion list stays on
screen with the keyboard open. Three things it guards against, all found by
measuring rather than by eye:

- **iOS auto-zoom.** Safari zooms the page in — and does not zoom back — when a
  focused input's font-size is under 16px. The guess box was 14.88px.
- **Tap targets.** The language buttons were 26px tall; interactive controls are
  now 44px on coarse pointers.
- **Keyboard occlusion.** On a 360×740 phone the first suggestion rendered below
  the fold once the keyboard opened. Focusing the guess box now scrolls the row
  to centre, and the list is height-capped and scrollable.

Layout uses `100dvh` and `env(safe-area-inset-*)` so the iOS URL bar and the
notch don't eat content.

## Known gap: the dataset has not been built

`build_dataset.py` has **not been run against the live endpoints.** The
environment this work was done in denies outbound access to every Wikimedia
host by egress policy:

```
query.wikidata.org      403  (policy denial at the egress proxy)
www.wikidata.org        403
commons.wikimedia.org   403
upload.wikimedia.org    403
en.wikipedia.org        403
api.wikimedia.org       403
```

So the pipeline is written, reviewed and tested against recorded fixtures, but
its live behaviour — real result counts, actual FR/ES coverage, Commons rate
limits, the true tier distribution — is unverified. Run it from a machine with
network access; `--limit 40` first.

Two things to check on that first run:

1. **The designation QIDs.** `Q9259` (World Heritage Site) and `Q1459900`
   (Intangible Cultural Heritage element) are the `P1435` values the pipeline
   queries. The script warns if a run returns under half the expected count,
   which is the signal that a QID is wrong.
2. **Translation coverage.** The run prints name and description coverage per
   language and warns if FR/ES fall below 60%. If most entries fall back to
   English names, that is worth deciding on deliberately rather than shipping
   as "trilingual".

## Deployment

`.github/workflows/pages.yml` publishes `unescle.html` as `index.html` on every
push to `main`, along with `data/` and `images/` if present. Enable Pages once,
under Settings → Pages → Source: **GitHub Actions**.

## Licences

Game code: yours. Map coastlines: Natural Earth via
[world-atlas](https://github.com/topojson/world-atlas) (ISC). Entry metadata:
Wikidata (CC0). Photos: individually licensed by their Commons contributors —
the licence and attribution ride with each entry and are displayed in-game.
