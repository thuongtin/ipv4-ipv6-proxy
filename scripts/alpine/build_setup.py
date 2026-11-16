#!/usr/bin/env python3
"""Build a deterministic, plain-text, standalone Alpine installer."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path

MODULES = ("manage.py", "ipv6_pool.py", "config.py", "ops.py")
MARKER = "__EMBEDDED_MODULES__"


def build(source_dir: Path, output: Path) -> None:
    template = (source_dir / "bootstrap.sh").read_text(encoding="utf-8")
    if template.count(MARKER) != 1:
        raise ValueError("bootstrap.sh must contain exactly one module marker")
    chunks = []
    for filename in MODULES:
        content = (source_dir / filename).read_text(encoding="utf-8")
        compile(content, filename, "exec")
        if not content.endswith("\n"):
            content += "\n"
        delimiter = "THREEPROXY_" + hashlib.sha256(content.encode()).hexdigest().upper()
        if delimiter in content.splitlines():
            raise ValueError(f"Here-document delimiter collision: {filename}")
        chunks.append(f'    cat > "$TOOLS/{filename}" <<\'{delimiter}\'\n{content}{delimiter}\n')
    rendered = template.replace(MARKER, "".join(chunks).rstrip("\n"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    output.chmod(0o755)


def main() -> None:
    source = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=source)
    parser.add_argument("--output", type=Path, default=source.parents[1] / "setup-alpine.sh")
    parser.add_argument("--check", action="store_true", help="Fail if the committed bundle differs from its modules")
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "setup-alpine.sh"
            build(args.source_dir, candidate)
            if not args.output.exists() or candidate.read_bytes() != args.output.read_bytes():
                raise SystemExit("Bundle is stale; run scripts/build.sh")
        print("BUNDLE_CHECK_PASS")
    else:
        build(args.source_dir, args.output)
        print(f"Built {args.output}")


if __name__ == "__main__":
    main()
