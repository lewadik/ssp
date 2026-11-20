"""Command line interface: init, sync, add, remove, certs, list, resolve, daemon, public-ip."""

from __future__ import annotations

import argparse
import os
import socket
import sys

from . import __version__
from .util import ConfigError, SsipngError, ServiceError

DEFAULT_CONFIG = "/etc/ssipng/config.toml"
DEV_CONFIG = "ssipng.toml"


def default_config_path() -> str:
    env = os.environ.get("SSIPNG_CONFIG")
    if env:
        return env
    if os.path.exists(DEFAULT_CONFIG):
        return DEFAULT_CONFIG
    if os.path.exists(DEV_CONFIG):
        return DEV_CONFIG
    return DEFAULT_CONFIG


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to config.toml (may appear before or after the subcommand)")
    p = argparse.ArgumentParser(
        prog="ssipng",
        description="Assign sslip.io domains to Proxmox VMs/CTs and wire them into the "
                    "web server on the PVE host.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version="ssipng " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common])

    sp = add("init", "write a starter config file")
    sp.add_argument("--path", default=None, help="config path (default %s)" % DEFAULT_CONFIG)
    sp.add_argument("--force", action="store_true", help="overwrite an existing file")
    sp.add_argument("--public-ip", default=None, help="public IP of this host")
    sp.add_argument("--detect-public-ip", action="store_true",
                    help="detect the public IP via api.ipify.org")

    sp = add("sync", "discover guests and assign missing domains")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-refresh", action="store_true",
                    help="do not re-resolve IPs of already managed guests")

    sp = add("add", "assign a domain to one vmid now")
    sp.add_argument("vmid", type=int)
    sp.add_argument("--ip", default=None, help="override the guest IP")
    sp.add_argument("--port", type=int, default=None, help="override the target port")
    sp.add_argument("--domain", default=None, help="override the hostname")
    sp.add_argument("--tls", default=None, help="override the TLS mode for this guest")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("remove", "remove the domain/vhost/state entry of a vmid or domain")
    sp.add_argument("target")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("certs", "run certbot for a managed guest and install the TLS vhost")
    sp.add_argument("vmid", type=int)

    add("list", "list managed guests")
    sp = add("resolve", "resolve a managed domain via DNS")
    sp.add_argument("target", help="vmid or hostname")

    add("daemon", "run the polling daemon (systemd service)")
    add("public-ip", "print this machine's public IP")
    return p


# --------------------------------------------------------------------------- #
# context helpers
# --------------------------------------------------------------------------- #

def _load_cfg(args, strict=True, required=True):
    from .config import Config, load
    path = getattr(args, "config", None) or default_config_path()
    if not os.path.exists(path):
        if not required:
            return Config()
        raise ConfigError("config file not found: %s (run `ssipng init` first)" % path)
    return load(path, strict=strict)


def _build_assigner(cfg):
    from .backends import from_config as backend_from_config
    from .proxmox import from_config as proxmox_from_config
    from .service import Assigner
    from .state import State
    from .util import Runner

    runner = Runner()
    proxmox = proxmox_from_config(cfg, runner)
    backend = backend_from_config(cfg, runner)
    state = State(cfg.daemon.state_file)
    return Assigner(cfg, proxmox, backend, state, runner, log=lambda m: print(m, flush=True))


def _print_report(report, dry_run: bool):
    if not report:
        print("no guests found in cluster resources")
        return
    print("%-6s %-9s %s" % ("VMID", "ACTION", "DOMAIN / MESSAGE"))
    errors = 0
    for item in report:
        errors += 1 if item.get("action") == "error" else 0
        print("%-6s %-9s %s" % (
            item.get("vmid", "-"), item.get("action", "-"),
            item.get("domain") or item.get("message") or ""))
    for item in report:
        for line in item.get("actions") or []:
            print("      · %s" % line)
    if dry_run:
        print("(dry run — nothing was written)")
    if errors:
        raise ServiceError("%d guest(s) failed; see messages above" % errors)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_init(args) -> int:
    from .config import write_example
    from .util import detect_public_ip

    path = args.path or (DEV_CONFIG if os.name == "nt" else DEFAULT_CONFIG)
    public_ip = args.public_ip
    if args.detect_public_ip and not public_ip:
        public_ip = detect_public_ip()
        print("detected public IP: %s" % public_ip)
    out = write_example(path, force=args.force, public_ip=public_ip)
    print("wrote %s" % out)
    print("next steps:")
    print("  1. edit %s — set [proxmox] token_secret and [domain] public_ip" % out)
    print("  2. create the API token:  pveum user token add root@pam ssipng --privsep 0")
    print("  3. check what would happen:  ssipng sync --dry-run")
    print("  4. apply now:  ssipng sync    (or run continuously:  ssipng daemon)")
    return 0


def cmd_sync(args) -> int:
    cfg = _load_cfg(args)
    assigner = _build_assigner(cfg)
    report = assigner.sync(dry_run=args.dry_run, refresh_ip=not args.no_refresh)
    _print_report(report, args.dry_run)
    return 0


def cmd_add(args) -> int:
    cfg = _load_cfg(args)
    assigner = _build_assigner(cfg)
    rec, actions = assigner.assign(args.vmid, dry_run=args.dry_run, ip=args.ip,
                                   port=args.port, domain=args.domain, tls=args.tls)
    for line in actions:
        print("· %s" % line)
    print("%s -> http%s://%s" % (args.vmid, "s" if rec.tls_mode != "none" else "", rec.domain))
    return 0


def cmd_remove(args) -> int:
    cfg = _load_cfg(args)
    assigner = _build_assigner(cfg)
    for line in assigner.remove(args.target, dry_run=args.dry_run):
        print("· %s" % line)
    return 0


def cmd_certs(args) -> int:
    cfg = _load_cfg(args)
    assigner = _build_assigner(cfg)
    rec, actions = assigner.refresh_certs(args.vmid)
    for line in actions:
        print("· %s" % line)
    print("TLS vhost active for %s" % rec.domain)
    return 0


def cmd_list(args) -> int:
    from .service import Record
    cfg = _load_cfg(args, strict=False)
    from .state import State
    state = State(cfg.daemon.state_file)
    entries = state.all()
    if not entries:
        print("no managed guests yet (try `ssipng sync`)")
        return 0
    print("%-6s %-5s %-14s %-18s %-45s %-10s" % ("VMID", "TYPE", "NAME", "IP", "DOMAIN", "TLS"))
    for key, raw in sorted(entries.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        rec = Record.from_dict(raw)
        print("%-6s %-5s %-14.14s %-18.18s %-45.45s %-10s" % (
            key, rec.proxmox_type, rec.name, rec.ip, rec.domain, rec.tls_mode))
    return 0


def cmd_resolve(args) -> int:
    cfg = _load_cfg(args, strict=False, required=False)
    target = args.target
    domain = target
    if str(target).isdigit():
        from .state import State
        state = State(cfg.daemon.state_file)
        entry = state.get(int(target))
        if not entry:
            raise ServiceError(f"vmid {target} is not managed (check `ssipng list`)")
        domain = entry.get("domain")
    try:
        infos = socket.getaddrinfo(domain, None)
    except OSError as e:
        print("DNS lookup failed for %s: %s" % (domain, e))
        print("(sslip.io names resolve globally; check reachability of resolvers)")
        return 1
    ips = sorted({i[4][0] for i in infos})
    print("%s -> %s" % (domain, ", ".join(ips)))
    return 0


def cmd_daemon(args) -> int:
    from .daemon import Daemon
    cfg = _load_cfg(args)
    daemon = Daemon(cfg, _build_assigner(cfg))
    daemon.run()
    return 0


def cmd_public_ip(args) -> int:  # noqa: ARG001
    from .util import detect_public_ip
    print(detect_public_ip())
    return 0


_COMMANDS = {
    "init": cmd_init, "sync": cmd_sync, "add": cmd_add, "remove": cmd_remove,
    "certs": cmd_certs, "list": cmd_list, "resolve": cmd_resolve,
    "daemon": cmd_daemon, "public-ip": cmd_public_ip,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except SsipngError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
