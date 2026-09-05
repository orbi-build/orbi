"""Guard the public repository license.

`orbi-build/orbi` is public, so the repository root must carry a license file
GitHub can surface. The project is fair-code under the Sustainable Use License
v1.0: free for internal and non-commercial use, commercial licence required
only to sell Orbi itself.

These tests fail when the file is missing, when the licence text drifts into a
home-grown variant (the limitation and patent clauses are what actually bind,
so they are asserted verbatim), or when the README stops explaining the terms
in plain language.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_FILE = REPO_ROOT / "LICENSE.md"
README_FILE = REPO_ROOT / "README.md"


def read_license() -> str:
    """The license text; fail fast when the file is missing."""
    assert LICENSE_FILE.is_file(), f"missing license file: {LICENSE_FILE}"
    return LICENSE_FILE.read_text(encoding="utf-8")


def test_license_exists_at_repo_root() -> None:
    """GitHub only surfaces a license from a root-level LICENSE file."""
    assert LICENSE_FILE.is_file(), "no LICENSE.md at the repository root"


def test_license_is_the_sustainable_use_license() -> None:
    """The file must carry the canonical Sustainable Use License v1.0."""
    text = read_license()
    assert "Sustainable Use License" in text, "missing the licence title"
    assert "Version 1.0" in text, "missing the version line"
    for clause in (
        "### Acceptance",
        "### Copyright License",
        "### Limitations",
        "### Patents",
        "### Notices",
        "### Termination",
        "### No Liability",
        "### Definitions",
    ):
        assert clause in text, f"missing licence section: {clause}"


def test_license_keeps_the_binding_limitation_verbatim() -> None:
    """The limitation clause is the whole point; it must not be reworded.

    Everything else in the licence is boilerplate shared with other fair-code
    projects. This sentence is what separates free internal use from a sale.
    """
    text = " ".join(read_license().split())
    assert (
        "You may use or modify the software only for your own internal business "
        "purposes or for non-commercial or personal use."
    ) in text, "the internal-use limitation has been altered"
    assert (
        "You may distribute the software or provide it to others only if you do "
        "so free of charge for non-commercial purposes."
    ) in text, "the distribution limitation has been altered"


def test_license_names_the_copyright_owner() -> None:
    """The licence must name a real copyright holder, not a placeholder."""
    text = read_license()
    assert "[name of copyright owner]" not in text, (
        "a template copyright placeholder is still in the licence"
    )
    assert re.search(r"Copyright \d{4} xqliu", text), (
        "no real copyright notice for the repository owner"
    )


def test_readme_explains_the_terms_in_plain_language() -> None:
    """A fair-code licence surprises people; the README must pre-empt that.

    Linking the file is not enough — the two questions every reader has are
    "can I use this at work for free" and "when do I owe you money".
    """
    assert README_FILE.is_file(), f"missing README: {README_FILE}"
    readme = README_FILE.read_text(encoding="utf-8")
    assert re.search(r"\]\(LICENSE\.md\)", readme), (
        "README does not link to the LICENSE.md file"
    )
    assert "Sustainable Use License" in readme, (
        "README does not name the Sustainable Use License"
    )
    assert "fair-code" in readme, "README does not say the project is fair-code"
    assert "free forever" in readme, (
        "README does not state that self-hosted use stays free"
    )
    assert "Commercial authorization" in readme, (
        "README does not say when a commercial licence is required"
    )
