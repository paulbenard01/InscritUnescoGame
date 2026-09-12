# assets

Brand files live here. The game loads the header mark from this folder.

| File | What it is | Used by |
|---|---|---|
| `heritlelogogold.svg` | the light mark, for the navy page | **the header** — `heritle.html` loads this exact path |
| `heritlelogonavy.svg` | the dark mark, for light backgrounds | share cards, print, anything on cream |
| `Heritlelogogold.png` | raster copy of the light mark | social preview (`og:image`), Apple touch icon |
| `Heritlelogonavy.png` | raster copy of the dark mark | spare |

Note the mixed capitalisation: the SVGs are lowercase and the PNGs are not.
That is how they were uploaded, and the code points at the real names rather
than renaming anyone's files. Worth normalising one day.

A `viewBox` was added to both SVGs, and nothing else was touched. Without one,
Safari will not scale an SVG inside an `<img>` — it renders at its native 673px
and gets clipped to the box. Chromium copes; Safari has never had to, and most
players are on a phone.

## Two things that will bite

**Capitalisation is part of the filename.** GitHub Pages serves from Linux, so
`Heritlelogogold.svg` and `heritlelogogold.svg` are different files. The page
asks for the first spelling exactly as written above.

**A missing or misnamed file is not a broken image.** The header hides the
`<img>` if it fails to load, so the page falls back to the wordmark alone. If
you upload and still see no mark, the filename does not match.

The mark is referenced once, in the header of `heritle.html`:

```html
<button class="mark" id="mark" aria-label="Heritle">
  <img src="assets/heritlelogogold.svg" alt="" onerror="…">
</button>
```

Changing which file the header uses means changing that one line.
