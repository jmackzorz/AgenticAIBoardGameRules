"""Verify that importing bgg_agentcore does not pull in ingestion-only packages."""

import importlib
import sys

import pytest

_FORBIDDEN = ["marker", "torch", "streamlit", "voyageai", "tiktoken", "langdetect"]


def _reimport(module: str) -> None:
    """Drop cached modules so the import is measured fresh, then import."""
    for key in list(sys.modules):
        if any(key == f or key.startswith(f + ".") for f in ["bgg_agentcore", *_FORBIDDEN]):
            del sys.modules[key]
    importlib.import_module(module)


def _assert_clean(module: str) -> None:
    loaded = set(sys.modules)
    for forbidden in _FORBIDDEN:
        contaminated = [m for m in loaded if m == forbidden or m.startswith(forbidden + ".")]
        assert not contaminated, (
            f"Importing {module} pulled in forbidden package '{forbidden}': {contaminated}"
        )


def test_agentcore_package_does_not_pull_ingestion_deps():
    _reimport("bgg_agentcore")
    _assert_clean("bgg_agentcore")


def test_agentcore_agent_does_not_pull_ingestion_deps():
    # The core logic layer — checked directly, not just via the package __init__.
    _reimport("bgg_agentcore.agent")
    _assert_clean("bgg_agentcore.agent")


def test_agentcore_sessions_does_not_pull_ingestion_deps():
    _reimport("bgg_agentcore.sessions")
    _assert_clean("bgg_agentcore.sessions")
