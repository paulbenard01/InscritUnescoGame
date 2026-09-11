# assets

Brand files live here. The game loads the header mark from this folder.

| File | What it is | Used by |
|---|---|---|
| `Heritlelogogold.svg` | the light mark, for the navy page | **the header** — `heritle.html` loads this exact path |
| `Heritlelogonavy.svg` | the dark mark, for light backgrounds | share cards, print, anything on cream |
| `Heritlelogogold.png` | raster copy of the light mark | social preview (`og:image`), Apple touch icon |
| `Heritlelogonavy.png` | raster copy of the dark mark | spare |

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
  <img src="assets/Heritlelogogold.svg" alt="" onerror="…">
</button>
```

Changing which file the header uses means changing that one line.
