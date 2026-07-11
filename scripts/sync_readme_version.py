from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
VERSION_LINE_RE = re.compile(r"^Current package version: `[^`]+`$", re.MULTILINE)
PYPROJECT_VERSION_RE = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)


def main() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    match = PYPROJECT_VERSION_RE.search(pyproject)
    if not match:
        raise SystemExit("pyproject.toml must contain a project version line.")
    version = match.group(1)
    text = README.read_text(encoding="utf-8")
    replacement = f"Current package version: `{version}`"
    updated, count = VERSION_LINE_RE.subn(replacement, text, count=1)
    if count != 1:
        raise SystemExit("README.md must contain exactly one 'Current package version' line.")
    README.write_text(updated, encoding="utf-8")
    print(replacement)


if __name__ == "__main__":
    main()
