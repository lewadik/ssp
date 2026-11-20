# ssipng

**Assign an external sslip.io domain to every newly created Proxmox VM/CT and wire it into the web server already running on the Proxmox host.**

[sslip.io](https://sslip.io/) (and its twin nip.io) is a public DNS service that returns the IP address embedded in a hostname: `203-0-113-5.sslip.io` resolves to `203.0.113.5`, and subdomain labels are allowed — so `web-1-101.203-0-113-5.sslip.io` also resolves to `203.0.113.5`. That makes zero-configuration external hostnames possible for anything you spin up on your Proxmox host.

## How it works

```
new VM/CT created
      │
      ▼
ssipng daemon (on the PVE host, polls the cluster every N seconds)
      ├─ discovers the guest IP   (QEMU guest agent / LXC interfaces / CT netX config / [targets] pin)
      ├─ builds the sslip.io name  web-1-101.203-0-113-5.sslip.io   → host public IP
      ├─ writes a vhost drop-in into the EXISTING web server (nginx / Apache / Caddy)
      │    server_name web-1-101.203-0-113-5.sslip.io → reverse_proxy 10.0.0.5:80
      ├─ validates config (nginx -t …), reloads the web server, rolls back on failure
      └─ records the mapping in /var/lib/ssipng/state.json
```

Result: `http://web-1-101.203-0-113-5.sslip.io` is reachable from the internet (given ports 80/443 reach the host), and every future VM/CT gets the same treatment automatically.

Two domain modes:

| mode | hostname | traffic path |
|---|---|---|
| `proxy` (default) | `{name}-{vmid}.{host_public_ip_dashed}.sslip.io` → host | host web server reverse-proxies to the guest |
| `direct` | `{guest_ip_dashed}.sslip.io` (optional `name-vmid.` label) | straight to the guest (guest IP must be publicly routable); a vhost is still created on the host |

## Requirements

- Proxmox VE 8+ host (Debian, Python ≥ 3.11 — stock `python3`, **no pip dependencies**)
- A web server already running on the host: **nginx**, **Apache 2** or **Caddy**
- Guests reachable for IP discovery: `qemu-guest-agent` inside VMs, or a static `ip=…` on the CT `netX` device, or a pinned IP in `[targets]`
- Internet reachability of ports 80/443 on the host public IP (port-forward or public NIC)

## Quick start

On the Proxmox host (as root):

```bash
# 1. copy or clone this repo onto the host, then:
packaging/install.sh

# 2. create an API token (or use password auth in the config)
pveum user token add root@pam ssipng --privsep 0

# 3. edit /etc/ssipng/config.toml: token_secret, [domain] public_ip, [web] backend
#    (ssipng init --detect-public-ip can fill the public IP for you)

# 4. preview, apply, then keep it running
ssipng sync --dry-run
ssipng sync
systemctl enable --now ssipng
```

`ssipng daemon` (the systemd service) polls `/cluster/resources` every 30 s; each new VM/CT is assigned a domain within one interval. Guest IP discovery is retried until the agent answers, so guests that are still booting are picked up automatically.

## CLI

```
ssipng init [--path P] [--public-ip IP] [--detect-public-ip] [--force]
ssipng sync [--dry-run] [--no-refresh]        # converge everything (create/refresh/cleanup)
ssipng add <vmid> [--ip IP] [--port P] [--domain D] [--tls MODE] [--dry-run]
ssipng remove <vmid|domain> [--dry-run]
ssipng certs <vmid>                           # run certbot for a managed guest, install TLS vhost
ssipng list                                   # table of managed guests
ssipng resolve <vmid|domain>                  # DNS lookup of the assigned name
ssipng daemon                                 # polling loop (systemd service)
ssipng public-ip                              # print this machine's public IP
```

Global options: `--config PATH` (before or after the subcommand), `SSIPNG_CONFIG` env var. Default config path: `/etc/ssipng/config.toml`.

## Configuration

Full annotated example: [config.example.toml](config.example.toml). Reference:

| section | key | default | meaning |
|---|---|---|---|
| `[proxmox]` | `mode` | `api` | `api` (HTTPS with token/password) or `cli` (`pvesh`/`qm`/`pct`, run as root on the host) |
| | `url` | `https://127.0.0.1:8006` | PVE API endpoint |
| | `token_id` / `token_secret` | — | `root@pam!ssipng` + secret (recommended) |
| | `password` | — | alternative to a token (ticket auth) |
| | `verify_ssl` | `false` | PVE ships a self-signed cert |
| `[domain]` | `mode` | `proxy` | `proxy` or `direct` (see table above) |
| | `public_ip` | — | **required in proxy mode** — the public IPv4/IPv6 of this host |
| | `suffix` | `sslip.io` | `sslip.io` / `nip.io` |
| | `template` | `{name}-{vmid}.{public_ip_dashed}.{suffix}` | placeholders: `name vmid public_ip public_ip_dashed ip_dashed suffix` |
| `[web]` | `backend` | `nginx` | `nginx` / `apache` / `caddy` |
| | `default_target_port` | `80` | guest port to proxy to |
| | `config_dir` | backend default | nginx `conf.d`, Apache `sites-available`, Caddy import dir |
| | `websocket` | `true` | nginx: `Upgrade`/`Connection` proxy headers |
| `[tls]` | `mode` | `none` | `none` / `selfsigned` / `certbot` / `auto` (auto = Caddy ACME, caddy only) |
| `[daemon]` | `interval` | `30` | seconds between cluster polls |
| | `cleanup` | `false` | drop domain+vhost when the guest is deleted from the cluster |
| | `include_tags` | `[]` | e.g. `["ssipng"]` — only manage guests carrying one of these PVE tags |
| | `exclude_vmids` | `[]` | never touch these guests |
| `[targets]` | `[targets.101]` | — | per-guest overrides: `ip` (pin, skips agent), `port`, `domain` (custom name), `tls` |

### Generated vhost (nginx example)

```nginx
# managed by ssipng — guest web-1-101.203-0-113-5.sslip.io (web-1, vmid 101); edits will be overwritten
map $http_upgrade $ssipng_ws_101 { default upgrade; '' close; }
server {
    listen 80;
    server_name web-1-101.203-0-113-5.sslip.io;
    location / {
        proxy_pass http://10.0.0.5:80;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $ssipng_ws_101;
        proxy_read_timeout 90s;
    }
}
```

One drop-in per guest: nginx `conf.d/ssipng-<vmid>.conf`, Apache `sites-available/ssipng-<vmid>.conf` (+ `a2ensite`), Caddy `conf.d/ssipng-<vmid>.caddy` (+ an `import` line appended to your Caddyfile once). Files are rewritten only when content changes; `nginx -t` / `apache2ctl -t` / `caddy validate` runs **before** every reload, and a failed validation automatically restores the previous vhost, so the web server never ends up with a broken config from ssipng.

## TLS

sslip.io **no longer publishes a wildcard certificate**, so TLS has to come from a CA or your own key pair:

| mode | backend | behavior |
|---|---|---|
| `none` | any | HTTP-only vhost (a `/.well-known/acme-challenge/` location is included for later certbot) |
| `selfsigned` | nginx / Apache / Caddy | generates `/etc/ssipng/tls/<domain>.{crt,key}` with openssl and serves HTTPS |
| `certbot` | nginx / Apache | HTTP-only vhost first; then `ssipng certs <vmid>` runs certbot webroot-01 and swaps in the 443 vhost (Let's Encrypt certifies sslip.io names via HTTP-01) |
| `auto` | Caddy only | bare hostname in the Caddyfile — Caddy obtains and renews certificates itself (the smoothest option) |

## Design notes

- **Why a polling daemon instead of a `hookscript`?** Proxmox hookscripts fire on start/stop phases, not on create — and a hookscript must be attached per VM in advance. Polling the cluster API catches *every* newly created VM/CT with no per-guest setup. `ssipng add <vmid>` exists for immediate, one-off assignment.
- **Idempotency**: identical vhost content is never rewritten; reloads happen only when something changed. Safe to run `sync` from cron *and* the daemon concurrently (state has a lock file).
- **IP changes**: on every sync, managed guests' IPs are re-resolved and the vhost is updated transparently (skip with `--no-refresh`; pinned `[targets].ip` never refreshes).
- **Least privilege**: the API only needs to read `/cluster/resources` and guest config/agent data; the vhost writing and reload happen locally as root. Use a dedicated token (`--privsep 0` for simplicity, or a role-limited user for stricter setups) and keep `/etc/ssipng/config.toml` at `0600`.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `no IP yet` in `ssipng sync` | guest still booting or no guest agent — install `qemu-guest-agent` in the VM, set static `ip=` on the CT netX, or pin `[targets.<vmid>].ip` |
| domain resolves but connection refused | ports 80/443 not forwarded/open on the host public IP; check the guest port (`default_target_port` / `--port`) |
| `certbot failed` | run `ssipng certs <vmid>` only after the HTTP vhost exists and `acme_webroot` is served (ssipng writes the ACME location for you); use `certbot_staging = true` while testing |
| Caddy: import glob error | the `00-keep.caddy` placeholder keeps the glob non-empty — don't delete it |
| daemon stuck `queued`-like silence | check `journalctl -u ssipng`; verify token secret and that `curl -k https://127.0.0.1:8006/api2/json/version` works from the host |

## Development

Pure stdlib Python — no pip install needed:

```bash
python run_tests.py -v          # 98 tests, unittest discovery
```

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11 / 3.12 / 3.13. Failures are mirrored into `$GITHUB_STEP_SUMMARY` with `::error::` annotations.

Project layout:

```
ssipng/
  cli.py           argparse CLI
  config.py        TOML config, validation, example text
  naming.py        sslip.io hostname construction + validation
  proxmox.py       REST API client + pvesh/qm/pct CLI fallback, IP discovery
  backends/        nginx / apache / caddy vhost renderers
  tls.py           selfsigned (openssl) + certbot helpers
  service.py       assign / remove / sync orchestration with rollback
  state.py         JSON state store with lock file
  daemon.py        polling loop (SIGTERM-clean, systemd-ready)
packaging/         install.sh + ssipng.service
tests/             unit tests (mocked Proxmox + backends, tmpdir state)
```

## License

MIT — see [LICENSE](LICENSE).
