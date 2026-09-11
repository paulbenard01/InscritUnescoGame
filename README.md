# Heritle

**[Play it →](https://paulbenard01.github.io/Heritle/)**

A daily guessing game about the world's inscribed heritage, in English, French
and Spanish. You get a photograph. You point at the map. Four rounds, once a
day, the same four for everybody.

---

## How to play

**You guess countries by pointing at them, never by naming the site.** There
are 2,166 inscriptions in the pool and most of them are places nobody could
name unprompted, so the name was never a fair thing to ask for. The map is the
puzzle.

A photograph appears with a ladder of locked clues. Tap the map where you think
it is — the tap proposes a country, and a second press commits it, so a stray
touch never costs you a guess.

Every guess tells you how far off you were, in which direction, and whether you
have the right continent and region. Clues unlock as you go: **Continent →
Category → Inscribed → Region → Description**. And each wrong guess uncovers
**another photograph** of the same place, because one photo is sometimes a
plaque or a doorway and that should cost you a guess, not the round.

### Scoring

| | |
|---|---|
| Name the country | **full marks**, and the round ends there |
| Run out of guesses | your **last** guess scores, by how close it was |

Only the last guess counts, so a third try is a commitment rather than a free
extra. A near miss keeps most of the round; the wrong continent keeps almost
none.

### A day

| Round | What | Guesses | Points |
|---|---|---|---|
| 1–3 | A World Heritage site, ramping from widely known to hidden gem | 3 each | 100 total |
| 4 | **Bonus** — a living tradition from the intangible heritage lists | 1 | 50 |

**150 for a perfect day.** Everything you meet is accessioned into your
collection with a number, your streak and averages go in the passport, and past
days stay playable in the archive as unscored practice.

---

## Where the content comes from

Everything is drawn from open data and rebuilt by a pipeline, not hand-curated.

- **1,262 World Heritage sites**, gated against the official inscription list by
  site number, so tentative-list candidates cannot slip in.
- **904 intangible elements** from the Representative List and the Urgent
  Safeguarding list.
- **5,871 photographs** from Wikimedia Commons, each carrying its own licence
  and photographer credit, shown in game beside the picture.
- **186 guessable countries**, every one of which you can actually point at.

An element inscribed by several countries — Nowruz has 23, falconry 26 —
accepts any of them.

---

## Running it yourself

The game is one HTML file with no build step. Open `heritle.html` directly and
it plays against a small built-in demo set. To play the real pool, serve the
folder so the browser will fetch `data/`:

```
python3 -m http.server 8000      # then open http://localhost:8000/heritle.html
```

### Rebuilding the data

Both builders need network access, so in practice they run in CI
(`.github/workflows/`) rather than on a laptop.

```
python build_dataset.py                          # Wikidata + Commons -> data/dataset.json
python build_dataset.py --fixture tests/fixtures # offline, against recorded responses
python tools/build_land.py                       # Natural Earth -> data/land.json
```

| file | what |
|---|---|
| `heritle.html` | the whole game |
| `build_dataset.py` | the inscription pipeline |
| `tools/build_land.py` | compiles the map geometry |
| `tools/compare_official.py` | audits the pool against the official lists |
| `tools/find_ich.py` | works out how the intangible lists are modelled |
| `data/dataset.json` | 2,166 entries (4.4 MB) |
| `data/land.json` | country polygons (2.3 MB) |

`data/land.json` carries 238 countries as rings keyed by ISO code. The game
draws its map from those same rings and hit-tests taps against them, so what
you see and what you can tap cannot drift apart.

### Tests

```
python tests/test_game.py     # end-to-end, desktop and five phone sizes
python tests/smoke.py         # a full day played through the UI
python tests/smoke.py --real  # the same, against the committed dataset
python tests/smoke.py --url https://…/   # against a deployed build
```

The suites play the game the way a person does — tapping the map rather than
calling functions — because the input is the part most likely to break, and
driving the game past it hides exactly that.

### Deploying

Pushing to `main` deploys via GitHub Actions (Settings → Pages → Source:
**GitHub Actions**). A second workflow then plays the deployed URL in a real
browser and fails if the live site does not behave.

---

## Licences

Game code: yours. Map coastlines and country borders: [Natural
Earth](https://www.naturalearthdata.com/) 10m admin-0 countries, public domain,
via [natural-earth-vector](https://github.com/nvkelso/natural-earth-vector).
Entry metadata: [Wikidata](https://www.wikidata.org/) (CC0). Photographs:
individually licensed by their Wikimedia Commons contributors — the licence and
attribution travel with each photograph and are shown beside it in game.

Heritle is an independent project. It is not affiliated with, endorsed by, or
connected to any heritage organisation; it simply reads public data about
inscribed places and traditions.
