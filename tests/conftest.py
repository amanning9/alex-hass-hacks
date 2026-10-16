"""Run from the repository root: uv run pytest tests

tests/custom_components/dreame_vacuum is a FAKE Dreame integration for tests only (don't
install it); Alex's Hacks is loaded from ../custom_components.
"""
import pathlib

import pytest
import custom_components

# The test plugin's own custom_components package is imported first; add ours to its path.
here = pathlib.Path(__file__).parent
custom_components.__path__[:0] = [str(here / "custom_components"), str(here.parent / "custom_components")]

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
