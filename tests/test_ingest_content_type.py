"""#224: the ingest CLI must infer content_type from file extension so code
files are routed to the code chunker (AST/regex) rather than the prose-text
chunker. Regression guard for the 'code ingested as prose' bug."""
from pathlib import Path

from helix_context.cli.cmd_ingest import _content_type_for, _CODE_EXTENSIONS


def test_code_extensions_route_to_code():
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
                ".c", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua", ".sh", ".sql"):
        assert _content_type_for(Path("mod" + ext)) == "code", ext


def test_text_and_markup_route_to_text():
    for ext in (".md", ".rst", ".txt", ".json", ".toml", ".yml", ".yaml", ".csv"):
        assert _content_type_for(Path("doc" + ext)) == "text", ext


def test_extension_match_is_case_insensitive():
    assert _content_type_for(Path("Mod.PY")) == "code"
    assert _content_type_for(Path("README.MD")) == "text"


def test_unknown_extension_defaults_to_text():
    assert _content_type_for(Path("x.weirdext")) == "text"
    assert _content_type_for(Path("noext")) == "text"


def test_default_extensions_with_code_suffix_are_code():
    # every code suffix listed in _CODE_EXTENSIONS resolves to "code"
    for ext in _CODE_EXTENSIONS:
        assert _content_type_for(Path("f" + ext)) == "code", ext
