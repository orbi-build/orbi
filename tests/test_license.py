"""Guard the public repository's dual license (Issue #1332).

Orbi is dual-licensed: every user picks either the GNU AGPL-3.0 (OSI open
source) or the Sustainable Use License (SUL) v1.0. GitHub's license detector
(licensee) reads every top-level `LICENSE*` / `COPYING*` file and reports
"Other" as soon as two different license texts sit there, so the root
`LICENSE` carries ONLY the verbatim GNU AGPL-3.0 text; the dual-license
explanation and the unchanged SUL text live in
`docs/licenses/sustainable-use-license.md` (a path GitHub does not treat as a
license file).

These tests fail when a second top-level license file appears, when the AGPL
text drifts from the GNU original (the pinned SHA-256), when the SUL
restriction or patent clauses are reworded, or when the packaging, READMEs or
CONTRIBUTING stop naming both licenses.
"""
import hashlib
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_FILE = REPO_ROOT / "LICENSE"
SUL_FILE = REPO_ROOT / "docs" / "licenses" / "sustainable-use-license.md"
README_FILE = REPO_ROOT / "README.md"
README_ZH_FILE = REPO_ROOT / "README.zh-CN.md"
CONTRIBUTING_FILE = REPO_ROOT / "CONTRIBUTING.md"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"

# The verbatim GNU AGPL-3.0 text from
# https://www.gnu.org/licenses/agpl-3.0.txt (661 lines, 34523 bytes). The
# hash was cross-checked against the Internet Archive raw snapshot of that
# exact URL and independent verbatim copies (Grafana, MinIO, Gentoo);
# SPDX's structured AGPL-3.0-only text carries the same URLs.
GNU_AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"

# PEP 639 SPDX expression for "AGPL-3.0 and SUL, user picks one". The SUL is
# not on the SPDX license list, so it is a `LicenseRef-` id.
SPDX_EXPRESSION = "AGPL-3.0-only OR LicenseRef-Sustainable-Use-1.0"


def read_license() -> str:
    """The root license text; fail fast when the file is missing."""
    assert LICENSE_FILE.is_file(), f"missing license file: {LICENSE_FILE}"
    return LICENSE_FILE.read_text(encoding="utf-8")


def readme_texts() -> dict[str, str]:
    texts = {}
    for path in (README_FILE, README_ZH_FILE):
        assert path.is_file(), f"missing README: {path}"
        texts[path.name] = path.read_text(encoding="utf-8")
    return texts


def test_root_license_is_the_only_top_level_license_file() -> None:
    """GitHub reports "Other" when two license files sit at the root."""
    top_level = sorted(
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_file()
        and path.name.startswith(("LICENSE", "LICENCE", "COPYING"))
    )
    assert top_level == ["LICENSE"], (
        f"the root must carry exactly one license file, found: {top_level}"
    )


def test_root_license_is_the_verbatim_gnu_agpl() -> None:
    """The file must be byte-identical to the GNU AGPL-3.0 text."""
    data = LICENSE_FILE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == GNU_AGPL_SHA256, (
        "the root LICENSE is not the verbatim GNU AGPL-3.0 text: "
        f"sha256={digest}"
    )


def test_root_license_carries_the_agpl_structure() -> None:
    """A hash alone hides a corrupted read; pin the visible GNU markers."""
    text = read_license()
    assert text.startswith(
        "                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 19 November 2007\n"
    ), "the root LICENSE does not open with the GNU AGPL-3.0 header"
    for marker in (
        "GNU AFFERO GENERAL PUBLIC LICENSE",
        "TERMS AND CONDITIONS",
        "13. Remote Network Interaction; Use with the GNU General Public License.",
        "How to Apply These Terms to Your New Programs",
        "https://www.gnu.org/licenses/",
    ):
        assert marker in text, f"the GNU AGPL text is missing: {marker!r}"


def test_sul_file_explains_the_dual_license() -> None:
    """The alternative license must stay discoverable and explained."""
    assert SUL_FILE.is_file(), f"missing license file: {SUL_FILE}"
    text = SUL_FILE.read_text(encoding="utf-8")
    assert "AGPL-3.0" in text, "the SUL file does not name the AGPL alternative"
    assert "Sustainable Use License" in text, "missing the SUL title"
    assert "Version 1.0" in text, "missing the SUL version line"
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
        assert clause in text, f"missing SUL section: {clause}"


def test_sul_restriction_and_patent_clauses_stay_verbatim() -> None:
    """The clauses that actually bind must not be reworded."""
    text = " ".join(SUL_FILE.read_text(encoding="utf-8").split())
    assert (
        "You may use or modify the software only for your own internal business "
        "purposes or for non-commercial or personal use."
    ) in text, "the internal-use limitation has been altered"
    assert (
        "You may distribute the software or provide it to others only if you do "
        "so free of charge for non-commercial purposes."
    ) in text, "the distribution limitation has been altered"
    assert (
        "The licensor grants you a license, under any patent claims the licensor "
        "can license, or becomes able to license, to make, have made, use, sell, "
        "offer for sale, import and have imported the software, in each case "
        "subject to the limitations and conditions in this license."
    ) in text, "the patent grant has been altered"


def test_sul_file_names_the_copyright_owner() -> None:
    """The license must name a real copyright holder, not a placeholder."""
    text = SUL_FILE.read_text(encoding="utf-8")
    assert "[name of copyright owner]" not in text, (
        "a template copyright placeholder is still in the license"
    )
    assert re.search(r"Copyright \d{4} xqliu", text), (
        "no real copyright notice for the repository owner"
    )


def test_pyproject_declares_the_dual_spdx_expression() -> None:
    """PyPI must show both licenses, with both texts included."""
    assert PYPROJECT_FILE.is_file(), f"missing packaging file: {PYPROJECT_FILE}"
    project = tomllib.loads(PYPROJECT_FILE.read_text(encoding="utf-8"))["project"]
    assert project["license"] == SPDX_EXPRESSION, (
        f"pyproject license is {project['license']!r}, expected {SPDX_EXPRESSION!r}"
    )
    license_files = project.get("license-files", [])
    assert "LICENSE" in license_files, (
        "pyproject must include the root AGPL LICENSE"
    )
    assert "docs/licenses/sustainable-use-license.md" in license_files, (
        "pyproject must include the SUL text"
    )


def test_readmes_describe_the_dual_license() -> None:
    """Both READMEs must say open source (AGPL) with the SUL alternative."""
    for name, text in readme_texts().items():
        assert "AGPL-3.0" in text, f"{name} does not name AGPL-3.0"
        assert "Sustainable Use License" in text, (
            f"{name} does not name the Sustainable Use License"
        )
        assert re.search(r"\]\(LICENSE\)", text), (
            f"{name} does not link the root AGPL LICENSE file"
        )
        assert "docs/licenses/sustainable-use-license.md" in text, (
            f"{name} does not link the SUL text"
        )
        assert "fair-code" not in text, (
            f"{name} still calls the project fair-code"
        )
    readmes = readme_texts()
    assert "open source" in readmes["README.md"], (
        "README.md does not say the project is open source"
    )
    assert "开源" in readmes["README.zh-CN.md"], (
        "README.zh-CN.md does not say the project is open source (开源)"
    )
    assert "free forever" in readmes["README.md"], (
        "README.md does not state that self-hosted use stays free"
    )
    assert "永久免费" in readmes["README.zh-CN.md"], (
        "README.zh-CN.md does not state that self-hosted use stays free"
    )


def test_contributing_covers_both_licenses_and_the_relicensing_clause() -> None:
    """Contributors license under both; the relicensing clause survives."""
    text = CONTRIBUTING_FILE.read_text(encoding="utf-8")
    assert "AGPL-3.0" in text, "CONTRIBUTING does not name AGPL-3.0"
    assert "Sustainable Use License" in text, (
        "CONTRIBUTING does not name the Sustainable Use License"
    )
    assert re.search(r"\]\(LICENSE\)", text), (
        "CONTRIBUTING does not link the root AGPL LICENSE file"
    )
    assert "docs/licenses/sustainable-use-license.md" in text, (
        "CONTRIBUTING does not link the SUL text"
    )
    assert "different licence in the future" in text, (
        "the relicensing clause has been removed"
    )
    assert "self-hosted use free of charge" in text, (
        "the relicensing clause lost its free-self-hosting condition"
    )
    assert "fair-code" not in text, "CONTRIBUTING still calls the project fair-code"
