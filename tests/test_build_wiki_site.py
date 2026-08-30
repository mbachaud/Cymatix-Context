"""
Tests for scripts/build_wiki_site.py.

Verifies that the wiki -> static site build:
  - Renders every wiki/*.md page except _Sidebar.md/_Footer.md to
    <site-dir>/public/wiki/<slug>/index.html (Home.md -> the /wiki/ index)
  - Rewrites intra-wiki hrefs (href="Page-Name" -> href="/wiki/page-name/",
    href="Home" -> href="/wiki/") while leaving external/anchor/asset hrefs
    alone
  - Copies wiki/assets/ to <site-dir>/public/wiki/assets/
  - Embeds the page-template chrome (header/brand/footer, stylesheet link,
    new Wiki nav link, title, canonical URL) around the rendered content
  - Renders markdown extensions (tables, fenced code)
  - Guards the `markdown` import with a friendly error instead of a raw
    traceback when the dependency is missing
  - Is drivable from the command line via main()
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build_wiki_site.py"


def _load_module():
    """Load scripts/build_wiki_site.py as a standalone module (it's a script,
    not part of the cymatix_context package, so it can't be imported normally)."""
    spec = importlib.util.spec_from_file_location("build_wiki_site", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load spec for {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _make_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "Home.md").write_text("# Home\n[go](Getting-Started)", encoding="utf-8")
    (wiki / "Getting-Started.md").write_text("# Getting Started\nhello", encoding="utf-8")
    (wiki / "_Sidebar.md").write_text("nav", encoding="utf-8")
    (wiki / "_Footer.md").write_text("footer nav", encoding="utf-8")
    return wiki


class TestBuildRendersPagesAndRewritesLinks:
    def test_build_renders_pages_and_rewrites_links(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        site = tmp_path / "site" / "public"
        site.mkdir(parents=True)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (site / "wiki" / "index.html").read_text(encoding="utf-8")
        assert 'href="/wiki/getting-started/"' in idx
        assert (site / "wiki" / "getting-started" / "index.html").exists()
        assert not (site / "wiki" / "_sidebar").exists()
        assert not (site / "wiki" / "_footer").exists()

    def test_home_link_rewrites_to_wiki_root(self, tmp_path, mod):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text("# Home\nhello", encoding="utf-8")
        (wiki / "Getting-Started.md").write_text(
            "# Getting Started\nBack to [Home](Home).", encoding="utf-8"
        )
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        page = (
            tmp_path / "site" / "public" / "wiki" / "getting-started" / "index.html"
        ).read_text(encoding="utf-8")
        assert 'href="/wiki/"' in page


class TestTitleAndCanonical:
    def test_home_page_title_and_canonical(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")
        assert "<title>Home — Cymatix Context wiki</title>" in idx
        assert '<link rel="canonical" href="https://cymatixcontext.com/wiki/">' in idx

    def test_subpage_title_and_canonical(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        page = (
            tmp_path / "site" / "public" / "wiki" / "getting-started" / "index.html"
        ).read_text(encoding="utf-8")
        assert "<title>Getting Started — Cymatix Context wiki</title>" in page
        assert (
            '<link rel="canonical" href="https://cymatixcontext.com/wiki/getting-started/">'
            in page
        )


class TestChromePreserved:
    def test_header_brand_footer_and_wiki_nav_link_present(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/style.css">' in idx
        assert 'class="app-header"' in idx
        assert 'class="brand-name">Cymatix Context</span>' in idx
        assert 'class="app-footer"' in idx
        assert '<a class="gh" href="/wiki/">' in idx
        assert '<main class="wiki-page">' in idx

    def test_external_anchor_and_asset_hrefs_untouched(self, tmp_path, mod):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text(
            "# Home\n"
            "[ext](https://example.com/Getting-Started)\n"
            "[anchor](#Getting-Started)\n"
            "[asset](assets/Getting-Started.png)\n",
            encoding="utf-8",
        )
        (wiki / "Getting-Started.md").write_text("# Getting Started\nhello", encoding="utf-8")
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")
        assert 'href="https://example.com/Getting-Started"' in idx
        assert 'href="#Getting-Started"' in idx
        assert 'href="assets/Getting-Started.png"' in idx

    def test_href_rewrite_scoped_to_anchor_tags_not_prose_or_code(self, tmp_path, mod):
        """Regression test (review finding, round 1): the intra-wiki href
        rewrite must only touch real <a href="..."> attributes. A literal
        occurrence of href="Home" inside inline code, or as plain prose
        text, is not a link and must survive byte-for-byte -- only the
        real markdown link should be rewritten."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text(
            "# Home\n\n"
            "Real link: [go](Getting-Started)\n\n"
            'Inline code sample: `href="Home"`\n\n'
            'Prose mention: the literal text href="Home" appears here too.\n',
            encoding="utf-8",
        )
        (wiki / "Getting-Started.md").write_text("# Getting Started\nhello", encoding="utf-8")
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")

        # The real markdown link is rewritten.
        assert '<a href="/wiki/getting-started/">go</a>' in idx

        # The inline-code literal survives verbatim -- not rewritten to /wiki/.
        assert '<code>href="Home"</code>' in idx

        # The bare-prose literal also survives verbatim.
        assert 'Prose mention: the literal text href="Home" appears here too.' in idx


class TestImageSrcRewrite:
    def test_asset_img_src_rewritten_code_literal_untouched(self, tmp_path, mod):
        """Controller-flagged plan gap: wiki pages embed diagrams as
        ![alt](assets/foo.svg), which markdown renders as
        <img src="assets/foo.svg">. That resolves relative to the page
        (/wiki/<slug>/), not to where wiki/assets/ actually lands
        (/wiki/assets/), so it must be rewritten the same tag-scoped way
        as the href fix -- and a literal src="assets/x.svg" written as
        inline code (e.g. a page documenting the embed syntax) must
        survive untouched."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text(
            "# Home\n\n"
            "![d](assets/x.svg)\n\n"
            'Inline code sample: `src="assets/x.svg"`\n',
            encoding="utf-8",
        )
        (wiki / "assets").mkdir()
        (wiki / "assets" / "x.svg").write_text("<svg></svg>", encoding="utf-8")
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")

        # The real image src is rewritten to resolve from /wiki/assets/.
        assert '<img alt="d" src="/wiki/assets/x.svg" />' in idx

        # The inline-code literal survives verbatim -- not rewritten.
        assert '<code>src="assets/x.svg"</code>' in idx

    def test_absolute_and_non_assets_img_srcs_untouched(self, tmp_path, mod):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text(
            "# Home\n\n"
            "![ext](https://example.com/diagram.png)\n\n"
            "![other](images/diagram.png)\n",
            encoding="utf-8",
        )
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")
        assert 'src="https://example.com/diagram.png"' in idx
        assert 'src="images/diagram.png"' in idx


class TestAssetsCopied:
    def test_copies_wiki_assets_directory(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        (wiki / "assets").mkdir()
        (wiki / "assets" / "diagram.svg").write_text("<svg></svg>", encoding="utf-8")
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        copied = tmp_path / "site" / "public" / "wiki" / "assets" / "diagram.svg"
        assert copied.exists()
        assert copied.read_text(encoding="utf-8") == "<svg></svg>"

    def test_no_assets_dir_does_not_error(self, tmp_path, mod):
        wiki = _make_wiki(tmp_path)
        # no wiki/assets/ created at all
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        assert (tmp_path / "site" / "public" / "wiki" / "assets").exists() is False


class TestMarkdownExtensions:
    def test_tables_and_fenced_code_render(self, tmp_path, mod):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text(
            "# Home\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n\n"
            "```python\nprint('hi')\n```\n",
            encoding="utf-8",
        )
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        idx = (tmp_path / "site" / "public" / "wiki" / "index.html").read_text(encoding="utf-8")
        assert "<table>" in idx
        assert "<pre>" in idx and "<code" in idx


class TestMissingMarkdownDependency:
    def test_missing_markdown_package_raises_friendly_error(self, tmp_path, mod, monkeypatch):
        wiki = _make_wiki(tmp_path)
        monkeypatch.setattr(mod, "markdown", None)
        monkeypatch.setattr(mod, "_MARKDOWN_IMPORT_ERROR", ImportError("no module named markdown"))
        with pytest.raises(RuntimeError, match="pip install markdown"):
            mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")


class TestMainCli:
    def test_main_parses_args_and_builds(self, tmp_path, mod, monkeypatch):
        wiki = _make_wiki(tmp_path)
        site_dir = tmp_path / "site"
        argv = [
            "build_wiki_site.py",
            "--wiki-dir",
            str(wiki),
            "--site-dir",
            str(site_dir),
        ]
        monkeypatch.setattr(sys, "argv", argv)
        rc = mod.main()
        assert rc == 0
        assert (site_dir / "public" / "wiki" / "index.html").exists()


class TestSidebarNav:
    def _make_wiki_with_sidebar(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text("# Home\nwelcome", encoding="utf-8")
        (wiki / "Getting-Started.md").write_text("# Getting Started\nhello", encoding="utf-8")
        (wiki / "_Sidebar.md").write_text(
            "**Start**\n\n- [Home](Home)\n- [Getting Started](Getting-Started)\n",
            encoding="utf-8",
        )
        return wiki

    def test_sidebar_rendered_with_rewritten_links_and_current_highlight(self, tmp_path, mod):
        wiki = self._make_wiki_with_sidebar(tmp_path)
        site = tmp_path / "site" / "public"
        site.mkdir(parents=True)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")

        sub = (site / "wiki" / "getting-started" / "index.html").read_text(encoding="utf-8")
        assert '<aside class="wiki-nav"' in sub
        assert 'href="/wiki/"' in sub  # Home link rewritten inside the nav
        # The current page's nav link is marked, the other page's is not.
        assert 'aria-current="page" href="/wiki/getting-started/"' in sub
        assert 'aria-current="page" href="/wiki/"' not in sub

        home = (site / "wiki" / "index.html").read_text(encoding="utf-8")
        assert 'aria-current="page" href="/wiki/"' in home
        assert 'aria-current="page" href="/wiki/getting-started/"' not in home

    def test_sidebar_mobile_details_block_present(self, tmp_path, mod):
        wiki = self._make_wiki_with_sidebar(tmp_path)
        site = tmp_path / "site" / "public"
        site.mkdir(parents=True)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        sub = (site / "wiki" / "getting-started" / "index.html").read_text(encoding="utf-8")
        assert '<details class="wiki-nav-mobile">' in sub
        assert "<summary>Pages</summary>" in sub

    def test_no_sidebar_file_builds_without_nav(self, tmp_path, mod):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Home.md").write_text("# Home\nwelcome", encoding="utf-8")
        site = tmp_path / "site" / "public"
        site.mkdir(parents=True)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        home = (site / "wiki" / "index.html").read_text(encoding="utf-8")
        assert '<aside class="wiki-nav"' not in home
        assert "wiki-nav-mobile" not in home

    def test_layout_wrapper_and_wide_shell_present(self, tmp_path, mod):
        wiki = self._make_wiki_with_sidebar(tmp_path)
        site = tmp_path / "site" / "public"
        site.mkdir(parents=True)
        mod.build(wiki_dir=wiki, site_dir=tmp_path / "site")
        sub = (site / "wiki" / "getting-started" / "index.html").read_text(encoding="utf-8")
        assert '<div class="wiki-layout">' in sub
        assert 'class="shell shell-wide"' in sub
