"""Guard the public repository license (Issue #81).

`xqliu/muyan-pilot` is public, so the repository root must carry a license
file GitHub can recognize. The Issue fixes Apache License 2.0 (SPDX
identifier `Apache-2.0`) as the default and requires the README to link to
the file. These tests fail when the file is missing, when it stops carrying
the canonical Apache-2.0 markers GitHub's detector relies on, or when the
README loses the link.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_FILE = REPO_ROOT / "LICENSE"
README_FILE = REPO_ROOT / "README.md"


def read_license() -> str:
    """The license text; fail fast when the file is missing."""
    assert LICENSE_FILE.is_file(), f"missing license file: {LICENSE_FILE}"
    return LICENSE_FILE.read_text(encoding="utf-8")


def test_license_exists_at_repo_root() -> None:
    """GitHub only detects a license from a root-level LICENSE file."""
    assert LICENSE_FILE.is_file(), "no LICENSE file at the repository root"


def test_license_is_canonical_apache_2() -> None:
    """The file must be the canonical Apache License 2.0 text.

    GitHub's license detection recognizes the exact canonical markers;
    these are the ones the detector keys on, so the file cannot drift
    into a home-grown variant.
    """
    text = read_license()
    assert "Apache License" in text, "missing the Apache License title"
    assert "Version 2.0, January 2004" in text, "missing the version line"
    assert "http://www.apache.org/licenses/" in text, (
        "missing the license URL header"
    )
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text, (
        "missing the terms section"
    )
    assert "8. Limitation of Liability" in text, "missing the liability clause"
    assert "9. Accepting Warranty or Additional Liability" in text, (
        "missing the accepting-warranty clause"
    )


def test_license_names_the_copyright_owner() -> None:
    """The placeholder copyright must be replaced with the real owner."""
    text = read_license()
    assert "Copyright [yyyy] [name of copyright owner]" not in text, (
        "the template copyright placeholder is still in the license"
    )
    assert re.search(r"Copyright \d{4} xqliu", text), (
        "no real copyright notice for the repository owner"
    )


def test_readme_links_to_the_license() -> None:
    """The README must link to the LICENSE file and name Apache License 2.0."""
    assert README_FILE.is_file(), f"missing README: {README_FILE}"
    readme = README_FILE.read_text(encoding="utf-8")
    assert re.search(r"\]\(LICENSE\)", readme), (
        "README does not link to the LICENSE file"
    )
    assert "Apache License 2.0" in readme, (
        "README does not name the Apache License 2.0"
    )
