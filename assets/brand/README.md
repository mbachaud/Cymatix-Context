# Cymatix brand assets

| File | What it is |
|---|---|
| `cymatix-mark.svg` | **Master mark.** Same geometry as the website and launcher header, accent `#ff9f43`. Every icon is rendered from this file. |
| `cymatix-mark-mono.svg` | Black version for macOS template tray icons (the OS tints them). |
| `cymatix-granulated.png` | The granulated (particle) thumbnail, for splash, installer and about screens. Hand-made, not generated. |
| `generated/` | Rendered outputs for the desktop app: `icon-512.png`, `icon.ico`, `icon.icns`, `tray-template-32.png`, `tray-{ok,warn,error}-32.png`, `badge-{ok,warn,error}.png`. |

The Python launcher's tray icons and favicon are rendered to
`cymatix_context/launcher/static/icons/` so they ship inside the package.

Generated files are committed but never hand-edited. Change the SVG, then:

```bash
python scripts/build_brand_icons.py
```

`tests/test_brand_assets.py` fails if a committed output no longer matches a
fresh render.
