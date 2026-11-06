# Darkroom brand mark

The owner chose logo option 3, "Negative": a cream film frame with five sprocket holes along the top and bottom, a dark window, and an amber sun and mountain. That choice is the design sign-off for the mark.

- `mark.svg`: the mark alone (viewBox 0 0 100 100), no background.
- `icon.svg`: the app icon, a full-bleed square with a gradient from #2a211a to #0f0c0a and the mark centred at about 65% of the tile. It has no rounded corners because iOS masks the App Store icon itself.

Where the PNGs come from, all rendered from these SVGs with headless Chromium (Playwright) and converted to RGB so none has an alpha channel:

- `frontend/public/icon-192.png`, `icon-512.png` and `apple-touch-icon.png` (180x180): `icon.svg` at those sizes.
- `frontend/public/favicon.svg` is the mark on a rounded tile of the same gradient; `favicon-32.png` is that file at 32x32.
