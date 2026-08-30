"""
Render wiki/*.md (a GitHub-wiki-shaped directory of markdown pages) into the
static site at <site-dir>/public/wiki/, wrapped in the site's page chrome.

Each wiki/<Page-Name>.md becomes <site-dir>/public/wiki/<slug>/index.html,
where slug is the lowercased filename stem (e.g. Getting-Started.md ->
public/wiki/getting-started/index.html). wiki/Home.md is special-cased to
the /wiki/ index itself (public/wiki/index.html), matching how GitHub
treats Home.md as the wiki's landing page. wiki/_Sidebar.md and
wiki/_Footer.md are GitHub-wiki chrome files, not content, and are skipped.
wiki/assets/ is copied verbatim to public/wiki/assets/.

Intra-wiki links written the normal markdown way -- [text](Page-Name), the
form that also resolves correctly on github.com/.../wiki -- are rewritten
to the site's URL scheme: href="Page-Name" -> href="/wiki/page-name/", and
href="Home" -> href="/wiki/". External URLs, anchors, and asset-relative
hrefs are left untouched (see _rewrite_intrawiki_hrefs).

The page chrome (header, brand mark, footer, /style.css link) is copied
verbatim from site/public/contributing/index.html, with a Wiki link added
to the header nav. See PAGE_TEMPLATE below.

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
DEFAULT_SITE_DIR = Path("F:/Projects/cymatix-context/site")

SKIP_PAGES = {"_Sidebar.md", "_Footer.md"}
MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "toc"]
CANONICAL_BASE = "https://cymatixcontext.com"

# Chrome copied verbatim from site/public/contributing/index.html (header,
# brand SVG, footer, /style.css link), plus a new "Wiki" nav link. Content
# is substituted via plain str.replace (not str.format) so that rendered
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
<div class="shell">
  <header class="app-header">
    <a class="brand" href="/">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 84 84">
          <path d="M24 12c24 8 24 52 0 60" />
          <path d="M60 12c-24 8-24 52 0 60" />
          <circle cx="24" cy="12" r="4" />
          <circle cx="60" cy="12" r="4" />
          <circle cx="32" cy="30" r="4" />
          <circle cx="52" cy="30" r="4" />
          <circle cx="52" cy="54" r="4" />
          <circle cx="32" cy="54" r="4" />
          <circle cx="60" cy="72" r="4" />
          <circle cx="24" cy="72" r="4" />
        </svg>
      </span>
      <span class="brand-name">Cymatix Context</span>
    </a>
    <div class="header-links">
      <a class="gh" href="/hosting/">
        <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="2.5" width="13" height="4.5" rx="1.2"/><rect x="1.5" y="9" width="13" height="4.5" rx="1.2"/></svg>
        Hosting
      </a>
      <a class="gh" href="/wiki/">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 2.5A.5.5 0 0 1 1.5 2H7a1 1 0 0 1 1 1v10.5a.5.5 0 0 1-.777.416A5.984 5.984 0 0 0 4 13H1.5a.5.5 0 0 1-.5-.5v-10Z"/><path d="M14.5 2H9a1 1 0 0 0-1 1v10.5a.5.5 0 0 0 .777.416A5.984 5.984 0 0 1 12 13h2.5a.5.5 0 0 0 .5-.5v-10a.5.5 0 0 0-.5-.5Z"/></svg>
        Wiki
      </a>
      <a class="gh" href="https://discord.gg/pX7x7pA3Da" target="_blank" rel="noopener">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.317 4.3698a19.7913 19.7913 0 0 0-4.8851-1.5152.0741.0741 0 0 0-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 0 0-.0785-.037 19.7363 19.7363 0 0 0-4.8852 1.515.0699.0699 0 0 0-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 0 0 .0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 0 0 .0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 0 0-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 0 1-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 0 1 .0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 0 1 .0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 0 1-.0066.1276 12.2986 12.2986 0 0 1-1.873.8914.0766.0766 0 0 0-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 0 0 .0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 0 0 .0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 0 0-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/></svg>
        Discord
      </a>
      <a class="gh" href="https://github.com/mbachaud/Cymatix-Context" target="_blank" rel="noopener">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
        GitHub
      </a>
      <a class="gh" href="https://github.com/mbachaud/Cymatix-Context/issues" target="_blank" rel="noopener">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z"/><path d="M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z"/></svg>
        Issues
      </a>
      <a class="gh" href="/contributing/">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z"/></svg>
        Contribute
      </a>
    </div>
  </header>
  <main class="wiki-page">
__CONTENT__
  </main>
  <footer class="app-footer">
    <span>Cymatix Context &middot; Apache-2.0 &middot; <a href="/">Home</a> &middot; <a href="/hosting/">Hosting</a></span>
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


def _render_page(title: str, canonical: str, content_html: str) -> str:
    return (
        PAGE_TEMPLATE.replace("__TITLE__", title)
        .replace("__CANONICAL__", canonical)
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

    written: list[Path] = []
    for page in pages:
        markdown_text = page.read_text(encoding="utf-8")
        title = _extract_title(markdown_text, page.stem)
        canonical = CANONICAL_BASE + href_targets[page.stem]
        content_html = markdown.markdown(markdown_text, extensions=MARKDOWN_EXTENSIONS)
        content_html = _rewrite_intrawiki_hrefs(content_html, href_targets)
        html = _render_page(title, canonical, content_html)

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
