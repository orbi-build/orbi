"""Muyan Pilot (Orbi) runtime package (Issue #168).

Standard src layout: the uv editable install maps this WHOLE package
directory onto the deployment checkout, so a newly added module in this
package is importable by the next CLI process without regenerating any
module list (the #158 stale-finder root cause).

The runtime entry points:

- `muyan_pilot.cli` — the `muyan-pilot` console script target
  (`muyan-pilot = "muyan_pilot.cli:main"` in pyproject.toml);
- `muyan_pilot.runner` — the Runner tick (`python3 -m
  muyan_pilot.runner --config ...`), the systemd ExecStart target
  through the installed console script.

The repo-root `muyan_pilot.py` is a thin direct-execution compatibility
shim (development path only); it is a flat file, never a package, and
the package lives in `src/`, so the checkout root cannot shadow the
installed package.
"""
__version__ = "0.2.0"
