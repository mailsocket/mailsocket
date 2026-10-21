# Asset Credits & Provenance

Everything in this static tree is either hand-authored for this project or
redistributed under the SIL Open Font License 1.1. Nothing is traced or copied
from any third-party logo or icon set.

## Original works (hand-drawn for mailsocket — no third-party source)

- `logo.svg` — full wordmark lockup. The "mailsocket" wordmark is set in
  JetBrains Mono ExtraBold and converted to vector outlines (embedded paths),
  composed with an original socket/grid mark. Mark geometry hand-drawn.
- `mark.svg` — square icon-only mark (rounded square + 2x2 socket grid).
- `favicon.svg` — same mark as `mark.svg`.
- `favicon.ico` — 32x32 raster fallback generated from `mark.svg`.
- `apple-touch-icon.png` — 180x180 raster generated from `mark.svg`.
- `og-image.png` — 1200x630 social card generated from `logo.svg` on the token
  background (`#0a0e0d`) with a green radial glow.
- `site.webmanifest` — authored config.
- Icon set in `../templates/dashboard/partials/_icons.html`
  (`wait` hourglass, `agents` bot, `braces`) — monoline SVG drawn by hand.

## Fonts (SIL OFL 1.1 — self-hosted, no CDN)

- `fonts/JetBrainsMono-{Regular,Medium,Bold,ExtraBold}.woff2`
  JetBrains Mono — Copyright 2020 The JetBrains Mono Project Authors
  (https://github.com/JetBrains/JetBrainsMono), release v2.304.
- `fonts/Inter-{Regular,Medium,SemiBold}.woff2`
  Inter — Copyright (c) 2016 The Inter Project Authors
  (https://github.com/rsms/inter), release v4.1.

Full license text lives in `fonts/OFL.txt`.
