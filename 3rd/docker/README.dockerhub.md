# Orbi Docker image

Orbi is an AI coding agent that runs on GitHub Issues. This image is a
**community-maintained wrapper, unmodified official deployment**: it runs the
same official systemd deployment described in the repository.

Use the image with the [Docker guide](https://docs.orbi.build/docker):

```bash
docker run -d --name orbi \
  --stop-signal SIGRTMIN+3 \
  --tmpfs /run --tmpfs /tmp \
  --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v orbi-deploy:/orbi \
  -v orbi-work:/work \
  -e GH_TOKEN="github_pat_xxx" \
  -e ORBI_SOURCE_REPO="OWNER/REPO" \
  ghcr.io/orbi-build/orbi:latest
```

- [Orbi repository](https://github.com/orbi-build/orbi)
- [Full Docker documentation](https://docs.orbi.build/docker)
