"""Optional Markdown dependency coverage for the interactive UI fallback."""

import builtins

import pytest


@pytest.fixture
def missing_markdown(monkeypatch):
    original_import = builtins.__import__

    def import_without_markdown(name, *args, **kwargs):
        if name == "markdown":
            raise ImportError("Markdown unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_markdown)
