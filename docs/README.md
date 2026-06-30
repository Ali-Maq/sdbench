# SDBench project website

The static landing page for SDBench, served via GitHub Pages from this `docs/`
folder. It is plain HTML + [Bulma](https://bulma.io/) CSS —
**no build step**. Everything in `docs/` is served as-is.

## Files

```
docs/
  index.html              # the single scrolling page
  README.md
  static/
    css/  bulma.min.css, index.css
    images/ PaperCover.jpg, figure1.png
```

You can **double-click `index.html`** to preview the page — no local server
required.

## Enabling GitHub Pages

Repository **Settings → Pages → Build and deployment**:

- **Source:** Deploy from a branch
- **Branch:** `main` (or whichever branch holds this `docs/`), folder **`/docs`**

The site then publishes at `https://microsoft.github.io/sdbench/`. No Actions
workflow is required. All asset paths are relative, so the site works under the
project sub-path and survives a repository rename.
