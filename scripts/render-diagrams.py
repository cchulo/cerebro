#!/usr/bin/env python3
"""Render every ```mermaid block in docs/ARCHITECTURE.md to docs/img/<name>.svg with mermaid-cli (mmdc).
Block order maps to NAMES below. Install once: npm i -g @mermaid-js/mermaid-cli"""
import pathlib, re, subprocess, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parent.parent
NAMES = ["system", "task-flow"]
md = (ROOT / "docs/ARCHITECTURE.md").read_text()
blocks = re.findall(r"```mermaid\n(.*?)```", md, re.S)
if len(blocks) != len(NAMES):
    sys.exit(f"expected {len(NAMES)} mermaid blocks, found {len(blocks)}; update NAMES")
(ROOT / "docs/img").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    for name, block in zip(NAMES, blocks):
        src = pathlib.Path(tmp) / f"{name}.mmd"; src.write_text(block)
        out = ROOT / "docs/img" / f"{name}.svg"
        subprocess.run(["mmdc", "-q", "-i", str(src), "-o", str(out), "-b", "#0b1220"], check=True)
        print("wrote", out.relative_to(ROOT))
