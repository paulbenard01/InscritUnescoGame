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

The game shell is finished and wired to the real dataset format, and the
pipeline has been validated against live Wikidata and Commons in samples — see
[What the live runs showed](#what-the-live-runs-showed). **The full dataset has
not been committed yet**: until `data/dataset.json` exists the game falls back
to its built-in 12-entry demo set and plays end-to-end on that.

To generate and publish it, run the **Build dataset** workflow with `commit`
checked (it needs to be on the default branch first, since `workflow_dispatch`
only registers from there).

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
- **One row per item.** Several `OPTIONAL`s together produce a cross product:
  three languages of alias crossed with images and criteria can turn one item
  into hundreds of near-identical rows. Multi-valued fields are
  `GROUP_CONCAT`'d and single-valued ones `SAMPLE`'d, so each item returns
  exactly one row. Aliases moved to their own per-page query — they were the
  worst offender.
- **Coordinates are sampled as a pair.** `lat` and `lon` are concatenated
  before sampling; an item with two `P625` statements could otherwise take its
  latitude from one and its longitude from the other, putting the pin in the
  sea.
- **Dual designations.** A few items are both a World Heritage Site and an
  intangible element. The first wins and each is logged — letting the second
  overwrite silently mislabelled the entry's type.
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

## What the live runs showed

The pipeline has been run against live Wikidata and Commons via the
`Build dataset` workflow — in samples, not yet a full crawl. From a 114-entry
run with photos:

| | |
|---|---|
| usable entries | 114 of 114 items, 0 dropped |
| photos found | 103 (90%) |
| coordinates from country centroid | 9 |
| entries with no continent | 0 |
| name coverage | EN 100% · **FR 67%** · **ES 54%** |
| description coverage | EN 98% · **FR 53%** · **ES 47%** |
| throughput | ~0.74 s/entry, so a full ~2,120-entry run is ~26 min |

Three things worth knowing, all of which only showed up against real data:

1. **A handful of items carry both designations.** Four appeared in the
   114-entry sample. The pipeline keeps the first and logs each one; before
   that it let the second silently overwrite the first, mislabelling the type.
2. **`P30` (continent) is almost never set on these items** — 112 of 114 had
   none. The country fallback is doing nearly all the work; without it the
   Continent clue tile would be blank for most entries.
3. **FR/ES coverage is partial, and this is the real answer to "is it
   trilingual?"** Names fall back to English for about a third of entries in
   French and half in Spanish; descriptions are worse. The clue tiles for
   continent, category and type are unaffected — those are codes translated in
   the UI — but names and descriptions are not. The run prints the numbers and
   warns under 60%. This is a content limitation of Wikidata, not a bug, and is
   worth an explicit decision rather than shipping quietly.

The designation QIDs `Q9259` and `Q1459900` are confirmed to return sensible
results. The script still warns if a run returns under half the expected count.

## Deployment

`.github/workflows/pages.yml` publishes `unescle.html` as `index.html` on every
push to `main`, along with `data/` and `images/` if present. Enable Pages once,
under Settings → Pages → Source: **GitHub Actions**.

## Licences

Game code: yours. Map coastlines: Natural Earth via
[world-atlas](https://github.com/topojson/world-atlas) (ISC). Entry metadata:
Wikidata (CC0). Photos: individually licensed by their Commons contributors —
the licence and attribution ride with each entry and are displayed in-game.
