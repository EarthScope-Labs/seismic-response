#!/usr/bin/env python3
"""Check a release tag against the package version, CITATION.cff and CHANGELOG.md, then write the release notes.

usage: release_notes.py TAG NOTES_FILE

TAG is `v` followed by the package version. The CHANGELOG section headed by that tag becomes NOTES_FILE.
Exits non-zero, naming the mismatch, if any of the three disagree with the tag or the section is empty.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    text = (ROOT / "src" / "seismic_response" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("error: no __version__ in src/seismic_response/__init__.py")
    return m.group(1)


def citation_version() -> str:
    text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    m = re.search(r"""^version:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", text, re.M)
    if not m:
        sys.exit("error: no version in CITATION.cff")
    return m.group(1)


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^##[ \t]+v?{re.escape(version)}(?:[ \t].*)?$\n(.*?)(?=^##[ \t]|\Z)", text, re.M | re.S)
    if not m:
        sys.exit(f"error: CHANGELOG.md has no '## v{version}' section")
    notes = m.group(1).strip()
    if not notes:
        sys.exit(f"error: CHANGELOG.md section 'v{version}' is empty")
    return notes


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    tag, out = sys.argv[1], Path(sys.argv[2])
    version = tag[1:] if tag.startswith("v") else tag
    problems = [
        f"{name} is {found}, tag is {tag}"
        for name, found in (("__version__", package_version()), ("CITATION.cff version", citation_version()))
        if found != version
    ]
    if not tag.startswith("v"):
        problems.append(f"tag {tag} does not start with 'v'")
    if problems:
        sys.exit("error: " + "; ".join(problems))
    out.write_text(changelog_section(version) + "\n", encoding="utf-8")
    print(f"{tag}: version checks passed, notes written to {out}")


if __name__ == "__main__":
    main()
