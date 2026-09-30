"""The platform-independent unit facts of the scheduler layer.

The naming rules (unit prefix, template and instance names), the
deployment placeholders carried by the unit templates and the config
declaration cap (Issue #827) are shared by EVERY member of the
scheduler layer — the interface/orchestration module
(:mod:`orbi.scheduler`) and both platform implementations
(:mod:`orbi.systemd_deploy`, :mod:`orbi.launchd_deploy`). They live
in this leaf so the members never need to import each other for a
constant or a name: the interface stays a one-way dispatcher and the
implementations stay leaves (the import cycle the layer was born
with, see ``tests/test_import_graph.py``).

This module imports nothing from ``orbi``.
"""


# The config declaration cap (Issue #827): a deployment may declare up to
# MAX_RUNNER_INSTANCES concurrent Runner instances. It is NOT a machine-
# capacity assertion and NOT derived from any unit-name list — the names
# are generated per count below, and the real concurrency boundary stays
# the flock slots in the Runner (max_concurrency).
MAX_RUNNER_INSTANCES = 5

# The unit templates are machine-independent. The machine-specific
# values (the deployment checkout path and the user home) are carried
# as placeholders and substituted at install time; the templates never
# hardcode machine paths.
REPO_DIR_PLACEHOLDER = "{{ORBI_REPO_DIR}}"
USER_HOME_PLACEHOLDER = "{{ORBI_USER_HOME}}"


def unit_prefix(unit_name: str | None = None) -> str:
    """The systemd-shape name prefix shared by every generated name."""
    return "orbi" if unit_name is None else f"orbi-{unit_name}"


def unit_names(unit_name: str | None = None) -> tuple[str, str]:
    prefix = unit_prefix(unit_name)
    return f"{prefix}@.service", f"{prefix}@.timer"


def timer_instances(unit_name: str | None = None,
                    count: int = 1) -> tuple[str, ...]:
    prefix = unit_prefix(unit_name)
    return tuple(f"{prefix}@{index}.timer" for index in range(1, count + 1))


def service_instances(unit_name: str | None = None,
                      count: int = 1) -> tuple[str, ...]:
    prefix = unit_prefix(unit_name)
    return tuple(f"{prefix}@{index}.service" for index in range(1, count + 1))
