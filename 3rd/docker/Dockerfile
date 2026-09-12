# Orbi one-container deployment — community-maintained, NOT officially
# supported (Issue #264). The core systemd deployment path is unchanged:
# this image hosts a real systemd (PID 1) + user manager so the official
# `systemd/orbi@.service` + `orbi@.timer` templates and `orbi setup` run
# unmodified. See 3rd/docker/README.md.
FROM python:3.14-slim

# python:3.14-slim puts Python 3.14 on PATH as python3 — the interpreter
# the unit template's ExecStartPre self-heal resolves via `--python
# python3`. Everything else is the official prerequisite set (git, gh,
# uv, the systemd user session, Node.js + Pi). systemd comes from Debian
# trixie (257, verified 2026-09-12); gh comes from its official apt
# repository (trixie ships 2.46, too old for the `blockedBy` query the
# runner uses) and Node.js 22 from the official NodeSource repository
# because pi requires node >= 22.19 while trixie ships 20.x.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git systemd dbus dbus-user-session \
      util-linux \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && gh --version | head -1 \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh \
 && bash /tmp/nodesource_setup.sh \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version

# uv (the CLI editable install tool; docs/getting-started.mdx prerequisite).
RUN pip install --no-cache-dir uv

# Pi, the development agent (docs/getting-started.mdx prerequisite table;
# installed with --ignore-scripts per the pi.dev docs). Lands on the
# system PATH, which the official unit template carries.
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent \
 && pi --version

# Container bootstrap: the entrypoint prepares volumes + auth + the CLI,
# then execs systemd; the oneshot unit starts the user manager and runs
# the official `orbi setup` at every container start (idempotent).
COPY docker-entrypoint.sh orbi-container-setup.sh /usr/local/bin/
COPY orbi-container-setup.service /etc/systemd/system/orbi-container-setup.service
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/orbi-container-setup.sh \
 && useradd --uid 1000 --create-home --shell /bin/bash orbi \
 && ln -s /etc/systemd/system/orbi-container-setup.service \
      /etc/systemd/system/multi-user.target.wants/orbi-container-setup.service \
 && ln -s /dev/null /etc/systemd/system/run-lock.mount

# State volumes: /orbi = deploy home (the orbi checkout, orbi.toml,
# .orbi/ state), /work = the delivery checkout (task worktrees live
# under /work/.worktrees/). The container itself stays stateless.
VOLUME ["/orbi", "/work"]

# systemd is PID 1; SIGRTMIN+3 is its shutdown signal.
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
