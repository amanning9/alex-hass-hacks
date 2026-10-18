"""Check the dev Python matches what the locked Home Assistant needs.

.python-version should be the Python minor version Home Assistant's own lower bound names, and
pyproject.toml's requires-python should be that same lower bound (so the lock resolves).
Renovate bumps both Python and the Home Assistant test harness on their own; this catches them
drifting apart and says what to change.

Run from the repository root: uv run --no-project tools/check_python.py
"""

import json
import pathlib
import re
import sys
import tomllib
import urllib.request

root = pathlib.Path(__file__).resolve().parent.parent
lock = tomllib.loads((root / "uv.lock").read_text())
ha_version = next(p["version"] for p in lock["package"] if p["name"] == "homeassistant")
with urllib.request.urlopen(f"https://pypi.org/pypi/homeassistant/{ha_version}/json", timeout=30) as response:
    ha_requires = json.load(response)["info"]["requires_python"]  # e.g. ">=3.14.2"
lower = re.search(r">=\s*(\d+)\.(\d+)(?:\.(\d+))?", ha_requires)
if lower is None:
    sys.exit(f"Can't read a lower bound from Home Assistant {ha_version}'s Requires-Python {ha_requires!r}")
want_minor = f"{lower[1]}.{lower[2]}"
want_requires = f">={lower[0][2:].strip()}"

have_minor = (root / ".python-version").read_text().strip()
have_requires = tomllib.loads((root / "pyproject.toml").read_text())["project"]["requires-python"]

problems = []
if have_minor != want_minor:
    problems.append(f".python-version is {have_minor}; set it to {want_minor}")
if have_requires.replace(" ", "") != want_requires:
    problems.append(f'pyproject.toml requires-python is "{have_requires}"; set it to "{want_requires}" (then uv lock)')
if problems:
    sys.exit(f"Home Assistant {ha_version} needs Python {ha_requires}:\n  " + "\n  ".join(problems))
print(f"OK: Home Assistant {ha_version} needs Python {ha_requires}; using {have_minor}.")
