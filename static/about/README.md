# About-page images

Drop photos here and they appear on `/about` automatically — the route checks
which of the expected filenames exist and renders a placeholder frame for the
rest, so a missing file is never a broken image.

Expected filenames (any that are present get used, in this order):

| File            | Caption shown under it                         |
|-----------------|------------------------------------------------|
| `desk.jpg`      | Where most of it was written                   |
| `early.jpg`     | An early version                               |
| `london.jpg`    | The city the catalogue covers                  |

## Before you commit one

Railway's filesystem is ephemeral — anything written at runtime is gone on the
next deploy — so these have to be committed to the repo, which makes their size
your page weight forever. Each one:

- **WebP or JPEG**, 1600px on the long edge is plenty
- **under 250KB** — `cwebp -q 78 in.jpg -o out.webp` usually lands well under
- landscape 3:2 works best with the layout

The `<img>` tags already carry `width`, `height` and `loading="lazy"`, so the
page does not reflow as they load.
