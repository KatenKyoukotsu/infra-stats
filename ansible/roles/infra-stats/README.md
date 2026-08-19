# Ansible Role: infra-stats (Production)

**Production-ready** deployment of the `infra-stats` service only.

Assumes **external dependencies** are already running:
- **VictoriaMetrics** — metrics storage (scrapes node-exporter + blackbox)
- **Node Exporter** — host metrics on target hosts
- **Blackbox Exporter** — HTTP probing, configured with `http_2xx` module

This role deploys **only the infra-stats container** with config pointing to external services.

## Quick Start

```bash
# 1. Copy inventory example
cp ansible/inventory.yml.example ansible/inventory.yml
# Edit ansible/inventory.yml with your server IPs

# 2. Edit playbook.yml with your config (targets, VM URL, BotX credentials)
#    Or override via -e @vars.yml

# 3. Deploy
ansible-playbook -i ansible/inventory.yml ansible/playbook.yml
```

## Directory Structure on Target

```
/opt/docker/infra-stats/
├── docker-compose.yml       # Generated (infra-stats service only)
├── configs/
│   └── config.yaml          # Generated from template
└── data/                    # SQLite storage (persisted)
    └── infra_stats.db
```

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `infra_stats_owner` / `infra_stats_group` | File/container UID:GID | `166535` |
| `infra_stats_build_local` | Build image locally vs pull | `false` |
| `infra_stats_app_dir` | Base directory | `/opt/docker/infra-stats` |
| `infra_stats_config` | Full config dict (see playbook.yml) | required |

## Config Structure (`infra_stats_config`)

```yaml
infra_stats_config:
  victoria_metrics:
    url: "http://victoria-metrics.prod:8428"  # External VM
  targets:
    - name: "prod-web"
      instance: "node-exporter.prod:9100"
      mountpoints: ["/", "/var/lib"]
      description: "Production web"
      url: "https://api.example.com/health"  # Blackbox probe
  analysis:
    cpu: true
    memory: true
    disk: true
    oom: true
    periods: ["1d", "7d", "14d"]
  blackbox:
    job: "blackbox"  # Must match VM scrape job name
  notifier:
    botx:
      enabled: true
      api_url: "https://botx.example.com/..."
      chat_id: "xxx"
      bearer_token: "xxx"
```

## How It Works

1. **Creates directories** `/opt/docker/infra-stats/{configs,data}` with correct ownership
2. **Cleans** app directory (`rm -rf`)
3. **Renders** `config.yaml` + `docker-compose.yml` from templates
4. **Stops** existing containers (`docker compose down --remove-orphans`)
5. **Pulls** image (or builds locally if `infra_stats_build_local: true`)
6. **Starts** service (`docker compose up -d`)
7. **Waits** for `/healthcheck` endpoint

## Security

- Container runs as non-root user (`infra_stats_owner:infra_stats_group`)
- `cap_drop: [ALL]`, `no-new-privileges:true`
- Read-only config mount
- JSON logging with rotation (10MB × 3 files)

## Health Checks

- Container healthcheck: `curl -f http://localhost:8080/healthcheck`
- API: `http://<host>:8080/api/status`
- Web UI: `http://<host>:8080/`

## Updating

```bash
# Pull new image and redeploy
ansible-playbook -i ansible/inventory.yml ansible/playbook.yml
```

## Building Locally

```bash
# Build from local Dockerfile (dev/test)
ansible-playbook -i ansible/inventory.yml ansible/playbook.yml -e infra_stats_build_local=true
```