# Muyan Pilot 可观测栈（Issue #162）

只读、独立、版本管理的观测层：

- `prometheus/muyan-pilot-exporter.py` — 只读读取 `muyan-pilot@*` 两个
  Runner service 的 user systemd journal（`journalctl --user -u
  'muyan-pilot@*' -o json`）和 `systemctl --user is-active`，在
  `127.0.0.1:9106/metrics` 暴露 Prometheus 指标。
- `grafana/dashboards/muyan-pilot.json` — Grafana Dashboard（uid
  `muyan-pilot`），展示两个 service、每个 slot 当前
  issue/role/phase/state、idle 秒数、`run_failed` by reason、idle 恢复
  步骤、`progress_publish_failed`、run 时长，并复用已有的 llama
  slot/context/TPS/token 指标（不重新实现）。

边界（与 Runner 完全解耦）：

- Runner 不知道 exporter 存在：不 push、不回调、不写任何状态。exporter /
  Prometheus / Grafana 任意一环挂掉都不影响 Issue 领取、Pi 运行、review、
  merge 或 fail fast。
- 没有数据库、队列、新状态存储；journal 是唯一数据源。
- 标签低基数：只用 `slot` / `repo` / `issue` / `role` / `phase` /
  `state` / `reason` / `result`；**永不**输出 `run_id`、branch、worktree、
  命令或 prompt 文本。每个 Runner 的维度用 `slot` 而不是 `instance`：
  `instance` 是 Prometheus 抓取器自己的标签，exporter 若输出 `instance`
  会被改名为 `exported_instance`，Dashboard 里所有 `sum by (instance)`
  和 `{{instance}}` 图例会按抓取目标 `127.0.0.1:9106` 而不是 Runner
  slot 分组（已对运行中的 Prometheus 验证：llama slot exporter 的
  `slot` 标签原样保留）。
- exporter 只读 journal 行契约（`LEVEL [run_id] <kind> key=value ...`，
  见 README「journal（本地，systemd）」）；未知 kind（`command=`、
  `stdout=` 等）跳过。journalctl 失败时 fail fast（带命令、rc、stderr），
  不吞错、不 fallback；HTTP 侧以 500 返回错误，scrape 失败在 Prometheus
  可见。

## 1. 运行 exporter

前台（开发/验证）：

```bash
/usr/bin/python3 monitoring/prometheus/muyan-pilot-exporter.py \
  --port 9106 --bind 127.0.0.1 --units 'muyan-pilot@*' --instances 1,2
```

验证：

```bash
curl -s 127.0.0.1:9106/health        # -> ok
curl -s 127.0.0.1:9106/metrics | head
```

常驻（可选，user systemd unit；仓库不管理该 unit，`install-units` 的
drift 机制只管 `muyan-pilot@.service` / `muyan-pilot@.timer` 两个模板）：

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/muyan-pilot-exporter.service <<'EOF'
[Unit]
Description=Muyan Pilot Prometheus exporter (Issue #162, read-only journal bridge)

[Service]
ExecStart=/usr/bin/python3 /home/xqianliu/Documents/muyan/muyan-pilot/monitoring/prometheus/muyan-pilot-exporter.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now muyan-pilot-exporter.service
```

参数：`--port`（默认 9106）、`--bind`（默认 127.0.0.1）、`--units`
（journalctl `-u` 模式，默认 `muyan-pilot@*`）、`--instances`（逗号分隔的
service 实例，默认 `1,2`）、`--cache-ttl`（journal 重读间隔秒数，默认 5）。

## 2. Prometheus 抓取

在 `/etc/prometheus/prometheus.yml` 的 `scrape_configs` 追加（与现有
`llama-slot-exporter` 等同款写法）：

```yaml
- job_name: muyan-pilot
  static_configs:
  - targets:
    - 127.0.0.1:9106
  metrics_path: /metrics
```

然后 `sudo systemctl reload prometheus`。验证：
`http://127.0.0.1:9090/targets` 中 `muyan-pilot` 为 UP。

## 3. Grafana Dashboard

Grafana（本机 `127.0.0.1:3000`）→ Dashboards → Import → 上传
`grafana/dashboards/muyan-pilot.json`（或
`/api/dashboards/import`），datasource 选 Prometheus（uid
`eflztqehr89a8c`）。Dashboard uid 固定为 `muyan-pilot`，重复导入即覆盖
同一 dashboard。

## 4. 测试

```bash
/usr/bin/python3 -m pytest tests/test_muyan_pilot_exporter.py \
  tests/test_muyan_pilot_dashboard.py -q
```

- exporter 测试钉住 journal 行契约、标签白名单（无 `run_id`/命令文本）、
  fail fast 行为和 HTTP 面（`/metrics`、`/health`、404、journal 错误 500）。
- dashboard 测试钉住 JSON 可重复导入、uid、schemaVersion、所有
  `muyan_pilot_*` 指标族与复用的 llama 指标都出现在查询里、表达式不使用
  白名单外标签、grid 位置不重叠。
