#!/usr/bin/env python3
"""Build dist/ovenlight.pyz: a single executable file (Python zipapp), standard library only.

usage: python3 scripts/build_pyz.py
"""
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    out = ROOT / "dist" / "ovenlight.pyz"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        shutil.copytree(ROOT / "ovenlight", stage / "ovenlight",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "__main__.py").write_text("import sys\nfrom ovenlight.cli import main\nsys.exit(main())\n")
        zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3")
    out.chmod(0o755)
    print("built", out.relative_to(ROOT), "(%d KB)" % (out.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
