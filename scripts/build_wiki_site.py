"""
Render wiki/*.md (a GitHub-wiki-shaped directory of markdown pages) into the
static site at <site-dir>/public/wiki/, wrapped in the site's page chrome.

Each wiki/<Page-Name>.md becomes <site-dir>/public/wiki/<slug>/index.html,
where slug is the lowercased filename stem (e.g. Getting-Started.md ->
public/wiki/getting-started/index.html). wiki/Home.md is special-cased to
the /wiki/ index itself (public/wiki/index.html), matching how GitHub
treats Home.md as the wiki's landing page. wiki/_Footer.md is GitHub-wiki
chrome and is skipped. wiki/_Sidebar.md is not rendered as a page either,
but it IS the navigation source: its markdown (the same grouped link list
GitHub shows beside every wiki page) is rendered into each page twice --
a sticky desktop <aside class="wiki-nav"> and a collapsible mobile
<details class="wiki-nav-mobile"> -- with the current page's link marked
class="current" + aria-current="page". If _Sidebar.md is absent the pages
build without a nav, single-column. wiki/assets/ is copied verbatim to
public/wiki/assets/.

Intra-wiki links written the normal markdown way -- [text](Page-Name), the
form that also resolves correctly on github.com/.../wiki -- are rewritten
to the site's URL scheme: href="Page-Name" -> href="/wiki/page-name/", and
href="Home" -> href="/wiki/". External URLs, anchors, and asset-relative
hrefs are left untouched (see _rewrite_intrawiki_hrefs).

Diagrams embedded the normal markdown way -- ![alt](assets/foo.svg) --
render to <img src="assets/foo.svg">, which resolves relative to the page
(/wiki/<slug>/), not to where wiki/assets/ actually lands
(/wiki/assets/). Those img srcs are rewritten too:
src="assets/foo.svg" -> src="/wiki/assets/foo.svg" (see
_rewrite_asset_img_srcs). Absolute URLs and non-assets/ srcs are left
untouched.

The page chrome (head, footer, /style.css link) matches the site's own
pages (public/about/index.html in the cymatix-website repo). The header is
left as an empty <header class="app-header"> placeholder: the site swaps
its shared chrome/header.html into every page at serve and build time, so
the nav lives in one place. See PAGE_TEMPLATE below.

Usage:
    python scripts/build_wiki_site.py [--wiki-dir DIR] [--site-dir DIR]

Requires the third-party `markdown` package (pip install markdown); the
import is guarded so running without it fails with a clear message instead
of a traceback deep in build().
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

try:
    import markdown
except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
    markdown = None
    _MARKDOWN_IMPORT_ERROR = exc
else:
    _MARKDOWN_IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"
DEFAULT_SITE_DIR = Path("F:/Projects/cymatix-website")

SKIP_PAGES = {"_Sidebar.md", "_Footer.md"}
MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "toc"]
CANONICAL_BASE = "https://cymatixcontext.com"

# Head and footer match the site's own pages; the header is the empty
# placeholder the site fills from chrome/header.html. Content is
# substituted via plain str.replace (not str.format) so that rendered
# markdown containing literal `{`/`}` -- code samples, JSON examples -- can
# never collide with template placeholders.
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — Cymatix Context wiki</title>
<meta name="description" content="Cymatix Context wiki: __TITLE__.">
<link rel="canonical" href="__CANONICAL__">
<link rel="stylesheet" href="/style.css">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 84 84'%3E%3Cg fill='none' stroke='%23ff9f43' stroke-width='6' stroke-linecap='round'%3E%3Cpath d='M24 12c24 8 24 52 0 60'/%3E%3Cpath d='M60 12c-24 8-24 52 0 60'/%3E%3C/g%3E%3Cg fill='%23ff9f43'%3E%3Ccircle cx='24' cy='12' r='6'/%3E%3Ccircle cx='60' cy='12' r='6'/%3E%3Ccircle cx='32' cy='30' r='6'/%3E%3Ccircle cx='52' cy='30' r='6'/%3E%3Ccircle cx='52' cy='54' r='6'/%3E%3Ccircle cx='32' cy='54' r='6'/%3E%3Ccircle cx='60' cy='72' r='6'/%3E%3Ccircle cx='24' cy='72' r='6'/%3E%3C/g%3E%3C/svg%3E">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#090c10">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f5f2ed">
</head>
<body>
<div class="shell shell-wide">
  <header class="app-header"><!-- filled from chrome/header.html by server.py and tools/build_site.py --></header>
  <div class="wiki-layout">
__SIDEBAR__    <main class="wiki-page">
__CONTENT__
    </main>
  </div>
  <footer class="app-footer">
    <span>Cymatix Context &middot; Apache-2.0 &middot; <a href="/">Home</a> &middot; <a href="/about/">About</a> &middot; <a href="/contact/">Contact</a> &middot; <a href="/privacy/">Privacy</a></span>
  </footer>
</div>
</body>
</html>
"""

# Only href attributes on actual <a ...> tags are candidates for the
# intra-wiki rewrite -- scoping the match to `<a\b...href="..."` (rather
# than a bare `href="..."` anywhere in the rendered HTML) keeps this from
# corrupting a literal `href="Home"` that shows up as prose or inside
# <code>/<pre> (e.g. a wiki page documenting markdown link syntax). Within
# the href value itself, only bare page-name text (letters, digits,
# hyphens -- no scheme, slash, or anchor) is even a candidate; that
# character class alone already excludes external URLs (contain "://"),
# anchors (contain "#"), and asset-relative paths (contain "/") -- see
# _rewrite_intrawiki_hrefs for the second gate (must also be a known page).
_HREF_RE = re.compile(r'(<a\b[^>]*\bhref=")([A-Za-z0-9-]+)(")')

# Same tag-scoped technique as _HREF_RE, for <img src="assets/...">: only
# src attributes on real <img ...> tags that start with the literal
# "assets/" prefix are rewritten -- a literal src="assets/x.svg" inside
# <code>/<pre> or prose never matches because it isn't inside an <img> tag.
# Absolute URLs (http://, https://, //cdn...) and non-assets/ relative
# srcs don't start with "assets/" and are left untouched.
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")(assets/[^"]*)(")')

_H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")


def _wiki_pages(wiki_dir: Path) -> list[Path]:
    """Markdown pages to render: wiki/*.md minus the GitHub-wiki chrome files."""
    return sorted(
        p for p in wiki_dir.glob("*.md") if p.name not in SKIP_PAGES
    )


def _slug(stem: str) -> str:
    return stem.lower()


def _href_targets(pages: list[Path]) -> dict[str, str]:
    """Map each page's raw filename stem (as it appears in markdown source,
    e.g. "Getting-Started") to its site-relative URL. Home.md is the /wiki/
    index itself rather than /wiki/home/."""
    targets: dict[str, str] = {}
    for page in pages:
        stem = page.stem
        targets[stem] = "/wiki/" if stem == "Home" else f"/wiki/{_slug(stem)}/"
    return targets


def _extract_title(markdown_text: str, stem: str) -> str:
    """Prefer the page's leading `# Heading`; fall back to a hyphen-split stem."""
    match = _H1_RE.search(markdown_text)
    if match:
        return match.group(1).strip()
    return stem.replace("-", " ").strip()


def _rewrite_intrawiki_hrefs(html_fragment: str, href_targets: dict[str, str]) -> str:
    def _replace(match: re.Match) -> str:
        prefix, stem, suffix = match.group(1), match.group(2), match.group(3)
        if stem in href_targets:
            return f"{prefix}{href_targets[stem]}{suffix}"
        return match.group(0)

    return _HREF_RE.sub(_replace, html_fragment)


def _rewrite_asset_img_srcs(html_fragment: str) -> str:
    def _replace(match: re.Match) -> str:
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        return f"{prefix}/wiki/{src}{suffix}"

    return _IMG_SRC_RE.sub(_replace, html_fragment)


def _render_sidebar(wiki_dir: Path, href_targets: dict[str, str]) -> str:
    """Render wiki/_Sidebar.md (the same grouped link list GitHub shows
    beside every wiki page) into the site's two nav blocks: a desktop
    <aside> and a mobile <details>. Returns "" when the file is absent, so
    pages build nav-less rather than failing."""
    sidebar_src = wiki_dir / "_Sidebar.md"
    if not sidebar_src.exists():
        return ""
    nav_html = markdown.markdown(
        sidebar_src.read_text(encoding="utf-8"), extensions=MARKDOWN_EXTENSIONS
    )
    nav_html = _rewrite_intrawiki_hrefs(nav_html, href_targets)
    return (
        '    <aside class="wiki-nav" aria-label="Wiki pages">\n'
        f"{nav_html}\n"
        "    </aside>\n"
        '    <details class="wiki-nav-mobile">\n'
        "      <summary>Pages</summary>\n"
        f"{nav_html}\n"
        "    </details>\n"
    )


def _mark_current(sidebar_html: str, page_url: str) -> str:
    """Mark the nav link pointing at page_url as the current page (in both
    the desktop and mobile nav blocks). The python-markdown renderer always
    emits intra-wiki nav anchors as exactly `<a href="...">` (href first,
    double-quoted), which _rewrite_intrawiki_hrefs preserves, so a plain
    string replace is exact here."""
    return sidebar_html.replace(
        f'<a href="{page_url}">',
        f'<a class="current" aria-current="page" href="{page_url}">',
    )


def _render_page(title: str, canonical: str, content_html: str, sidebar_html: str = "") -> str:
    return (
        PAGE_TEMPLATE.replace("__TITLE__", title)
        .replace("__CANONICAL__", canonical)
        .replace("__SIDEBAR__", sidebar_html)
        .replace("__CONTENT__", content_html)
    )


def _output_path(public_wiki_dir: Path, stem: str) -> Path:
    if stem == "Home":
        return public_wiki_dir / "index.html"
    return public_wiki_dir / _slug(stem) / "index.html"


def build(wiki_dir: Path | str, site_dir: Path | str) -> list[Path]:
    """Render every wiki/*.md page (minus _Sidebar.md/_Footer.md) into
    <site_dir>/public/wiki/, and copy wiki/assets/ alongside them.

    Returns the list of index.html paths written.
    """
    if markdown is None:
        raise RuntimeError(
            "The 'markdown' package is required to build the wiki site.\n"
            "Install it with: pip install markdown"
        ) from _MARKDOWN_IMPORT_ERROR

    wiki_dir = Path(wiki_dir)
    site_dir = Path(site_dir)
    public_wiki_dir = site_dir / "public" / "wiki"
    public_wiki_dir.mkdir(parents=True, exist_ok=True)

    pages = _wiki_pages(wiki_dir)
    href_targets = _href_targets(pages)
    sidebar_html = _render_sidebar(wiki_dir, href_targets)

    written: list[Path] = []
    for page in pages:
        markdown_text = page.read_text(encoding="utf-8")
        title = _extract_title(markdown_text, page.stem)
        canonical = CANONICAL_BASE + href_targets[page.stem]
        content_html = markdown.markdown(markdown_text, extensions=MARKDOWN_EXTENSIONS)
        content_html = _rewrite_intrawiki_hrefs(content_html, href_targets)
        content_html = _rewrite_asset_img_srcs(content_html)
        page_sidebar = _mark_current(sidebar_html, href_targets[page.stem]) if sidebar_html else ""
        html = _render_page(title, canonical, content_html, page_sidebar)

        out_path = _output_path(public_wiki_dir, page.stem)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        written.append(out_path)

    assets_src = wiki_dir / "assets"
    if assets_src.exists():
        assets_dst = public_wiki_dir / "assets"
        shutil.copytree(assets_src, assets_dst, dirs_exist_ok=True)

    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--wiki-dir",
        default=str(DEFAULT_WIKI_DIR),
        help=f"source wiki/ directory (default: {DEFAULT_WIKI_DIR})",
    )
    ap.add_argument(
        "--site-dir",
        default=str(DEFAULT_SITE_DIR),
        help=f"site checkout root, containing public/ (default: {DEFAULT_SITE_DIR})",
    )
    args = ap.parse_args(argv)

    wiki_dir = Path(args.wiki_dir)
    if not wiki_dir.is_dir():
        print(f"error: wiki directory not found: {wiki_dir}", file=sys.stderr)
        return 1

    try:
        written = build(wiki_dir=wiki_dir, site_dir=Path(args.site_dir))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {len(written)} wiki page(s) under {Path(args.site_dir) / 'public' / 'wiki'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
