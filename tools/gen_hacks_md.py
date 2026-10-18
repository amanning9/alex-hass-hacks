"""Regenerate HACKS.md from each hack's KEY/TITLE/WHAT/WHY/REMOVE_WHEN.

Run from the repository root: uv run tools/gen_hacks_md.py   (no Home Assistant needed)
"""

import ast
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
hacks_dir = root / "custom_components" / "alex_hacks" / "hacks"
FIELDS = ("KEY", "TITLE", "WHAT", "WHY", "REMOVE_WHEN")

out = [
    "# Alex's Hacks",
    "",
    "Each hack is separate: added on its own in Home Assistant (Settings → Devices & services →",
    "Add integration → Alex's Hacks), and removable on its own once upstream catches up.",
    "Generated from the hack modules by `tools/gen_hacks_md.py`; edit the modules, not this file.",
    "",
]
for path in sorted(hacks_dir.glob("*.py")):
    tree = ast.parse(path.read_text())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in FIELDS:
                values[name] = ast.literal_eval(node.value)
    if not all(field in values for field in FIELDS):
        continue
    out += [
        f"## {values['TITLE']} (`{values['KEY']}`)",
        "",
        f"**What:** {values['WHAT']}",
        "",
        f"**Why:** {values['WHY']}",
        "",
        f"**Remove when:** {values['REMOVE_WHEN']}",
        "",
    ]
(root / "HACKS.md").write_text("\n".join(out))
print("wrote", root / "HACKS.md")
