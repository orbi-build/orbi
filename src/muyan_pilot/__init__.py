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

The checkout root carries NO `muyan_pilot.py`: a flat file named like
the package would shadow the installed package for every process with
the checkout root on sys.path. The direct-execution compatibility entry
is `python3 -m muyan_pilot.cli` (development path only).
"""
__version__ = "0.2.0"
