# Ember (vendored)

Ember design system, copied unchanged on 2026-10-30 from the local checkout at
`C:\Users\david\home\Ember` (the source files were last modified 2026-10-21; that folder is
not a git repository, so there is no commit to cite).

| This folder | Copied from |
|---|---|
| `tokens.css` | `tokens.css` |
| `bundle.css` | `components/bundle.css` |
| `fonts/*` (three variable `.woff2` fonts and `OFL.txt`) | `fonts/*` |
| `icons/*.svg` (45 icons) | `assets/Icons/*.svg` |

`fonts/` stays next to `tokens.css` because the `@font-face` rules there use relative
`url("fonts/...")` paths.

Do not edit these files by hand. To update Ember, copy them again from the source and note the
new date here. App-specific styles belong in `../app.css`.

The fonts are licensed under the SIL Open Font License (`fonts/OFL.txt`).

The htmx library is vendored separately, see `../vendor/SOURCE.md`.
