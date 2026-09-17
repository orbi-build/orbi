# Orbi Docker image

[Orbi](https://orbi.build) is an AI coding agent that runs on GitHub Issues: label an Issue `ai-ready` and it implements, tests, reviews in a separate session, merges and tags. This image is a **community-maintained wrapper around the unmodified official deployment**: systemd as PID 1 running the same units the systemd install uses.

Tags: `latest` and the release number without the `v` (e.g. `0.5.18`), for `linux/amd64` and `linux/arm64`. The same image is on GHCR as `ghcr.io/orbi-build/orbi`.

One `docker run` reaches a real delivery. Substitute the three quoted values: a GitHub token with write access to the task-pool repo, the repo as `OWNER/REPO`, and the model key. DeepSeek is the example; any OpenAI-compatible endpoint works through the same four `ORBI_PI_*` variables.

```bash
# Optional model limits with their defaults — add them as -e lines to the
# command below (before the image reference) only to override:
#   -e ORBI_PI_API=openai-completions
#   -e ORBI_PI_CONTEXT_WINDOW=128000
#   -e ORBI_PI_MAX_TOKENS=16384
docker run -d --name orbi \
  --stop-signal SIGRTMIN+3 \
  --tmpfs /run --tmpfs /tmp \
  --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v orbi-deploy:/orbi \
  -v orbi-work:/work \
  -e GH_TOKEN="github_pat_xxx" \
  -e ORBI_SOURCE_REPO="OWNER/REPO" \
  -e ORBI_PI_PROVIDER=deepseek \
  -e ORBI_PI_MODEL=deepseek-chat \
  -e ORBI_PI_BASE_URL=https://api.deepseek.com \
  -e ORBI_PI_API_KEY="sk-xxx" \
  docker.io/orbibuild/orbi:latest
```

Then open an Issue in `OWNER/REPO` and add the `ai-ready` label. Within two 5-minute ticks a PR opens; it merges after the review round. All state lives in the two named volumes.

- [Orbi](https://orbi.build): what it is and why, self-hosted or cloud
- [Full Docker guide](https://docs.orbi.build/docker): every variable, the two volumes, the token permissions, and the Orbi quick guide (Issue shape, labels, releases)
- [Source of this image](https://github.com/orbi-build/orbi/tree/main/3rd/docker)
- [Orbi repository](https://github.com/orbi-build/orbi)
