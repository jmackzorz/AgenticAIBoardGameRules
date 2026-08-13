"""Verify that importing bgg_lambda does not pull in ingestion-only packages."""

import importlib
import sys

import pytest

_FORBIDDEN = ["marker", "torch", "streamlit", "voyageai", "tiktoken", "langdetect"]


def test_lambda_handler_import_does_not_pull_ingestion_deps():
    # Clear any previously imported modules so the import is fresh.
    for key in list(sys.modules):
        if any(key == f or key.startswith(f + ".") for f in ["bgg_lambda", *_FORBIDDEN]):
            del sys.modules[key]

    # This must succeed without pulling in heavy ingestion deps.
    importlib.import_module("bgg_lambda")

    loaded = set(sys.modules)
    for forbidden in _FORBIDDEN:
        contaminated = [m for m in loaded if m == forbidden or m.startswith(forbidden + ".")]
        assert not contaminated, (
            f"Importing bgg_lambda pulled in forbidden package '{forbidden}': {contaminated}"
        )


def test_lambda_package_agent_does_not_pull_ingestion_deps():
    # Also verify the agent module directly — it's the core logic layer.
    for key in list(sys.modules):
        if any(key == f or key.startswith(f + ".") for f in ["bgg_lambda", *_FORBIDDEN]):
            del sys.modules[key]

    importlib.import_module("bgg_lambda.agent")

    loaded = set(sys.modules)
    for forbidden in _FORBIDDEN:
        contaminated = [m for m in loaded if m == forbidden or m.startswith(forbidden + ".")]
        assert not contaminated, (
            f"Importing bgg_lambda.agent pulled in forbidden package '{forbidden}': {contaminated}"
        )
