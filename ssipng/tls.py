"""TLS helpers: self-signed certificate generation (openssl) and certbot HTTP-01 issuance.

Note: sslip.io no longer publishes a wildcard certificate, so TLS must come from
Let's Encrypt HTTP-01 (certbot), a self-signed pair, or Caddy's built-in ACME ("auto").
"""

from __future__ import annotations

from pathlib import Path

from .util import ConfigError, ServiceError, ensure_dir

TLS_MODES = ("none", "selfsigned", "certbot", "auto")


def check_mode(mode: str, backend: str):
    if mode not in TLS_MODES:
        raise ConfigError("tls.mode must be one of %s" % ", ".join(TLS_MODES))
    if mode == "auto" and backend != "caddy":
        raise ConfigError("tls.mode 'auto' only works with the caddy backend")
    if mode == "certbot" and backend == "caddy":
        raise ConfigError("tls.mode 'certbot' is not used with caddy (use 'auto' or manual certs)")


def cert_paths(tls_cfg, domain: str, mode: str | None = None):
    """(cert, key) paths for selfsigned/certbot modes; None for none/auto.
    `mode` overrides tls_cfg.mode (per-guest TLS override).

    Uses plain string joins so rendered vhost content stays POSIX-style on any
    build machine (the target host is always Linux)."""
    mode = mode or tls_cfg.mode
    if mode == "selfsigned":
        base = str(tls_cfg.selfsigned_dir).rstrip("/")
        return f"{base}/{domain}.crt", f"{base}/{domain}.key"
    if mode == "certbot":
        base = f"/etc/letsencrypt/live/{domain}"
        return f"{base}/fullchain.pem", f"{base}/privkey.pem"
    return None


def generate_selfsigned(domain: str, out_dir, runner, existing_check=True) -> tuple[str, str]:
    """Create a self-signed cert for `domain` with openssl (idempotent)."""
    ensure_dir(out_dir)
    base = str(out_dir).rstrip("/")
    crt, key = f"{base}/{domain}.crt", f"{base}/{domain}.key"
    if existing_check and Path(crt).exists() and Path(key).exists():
        return crt, key
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
        "-days", "825", "-subj", f"/CN={domain}",
        "-addext", f"subjectAltName=DNS:{domain}",
        "-keyout", key, "-out", crt,
    ]
    r = runner.run(cmd)
    if r.rc != 0:
        raise ServiceError(f"openssl self-signed cert generation failed: {r.combined}")
    return crt, key


def certbot_issue(domain: str, webroot: str, email: str | None = None,
                  staging: bool = False, runner=None) -> str:
    """Obtain a Let's Encrypt certificate via the HTTP-01 webroot challenge."""
    if runner is None:
        from .util import Runner
        runner = Runner()
    if not Path(webroot).exists():
        raise ServiceError(
            f"ACME webroot {webroot} does not exist; create it and make sure the HTTP vhost "
            "serves /.well-known/acme-challenge/ from it (ssipng writes that location for you)"
        )
    cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos",
           "--webroot", "-w", webroot, "-d", domain]
    cmd += ["--email", email] if email else ["--register-unsafely-without-email"]
    if staging:
        cmd.append("--staging")
    r = runner.run(cmd)
    if r.rc != 0:
        raise ServiceError(f"certbot failed: {r.combined}")
    return f"/etc/letsencrypt/live/{domain}/fullchain.pem"
