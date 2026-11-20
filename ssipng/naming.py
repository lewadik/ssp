"""sslip.io / nip.io hostname construction and validation.

sslip.io returns the IP embedded in a hostname:
    192.168.1.10.sslip.io  /  192-168-1-10.sslip.io      -> 192.168.1.10
    web-101.203-0-113-5.sslip.io                         -> 203.0.113.5  (subdomain label allowed)
    2a01-4f8-c17-b8f--2.sslip.io                         -> 2001:4f8:c17:b8f::2 (IPv6: dashes only)
"""

from __future__ import annotations

import ipaddress
import re

from .util import ConfigError

_LABEL_RE = re.compile(r"[^a-z0-9-]+")
_MULTI_DASH = re.compile(r"-{2,}")

#: placeholders allowed in [domain].template
PLACEHOLDERS = ("name", "vmid", "public_ip", "public_ip_dashed", "ip_dashed", "suffix")


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase DNS-safe label from a guest name (VM names may contain spaces/underscores)."""
    s = (text or "").strip().lower().replace("_", "-").replace(" ", "-")
    s = _LABEL_RE.sub("-", s)
    s = _MULTI_DASH.sub("-", s).strip("-")
    s = s[:max_len].strip("-")
    return s or "vm"


def ip_dashed(ip: str) -> str:
    """'203.0.113.5' -> '203-0-113-5'; IPv6 uses the compressed form with ':' -> '-'."""
    a = ipaddress.ip_address(ip)
    if a.version == 4:
        return str(a).replace(".", "-")
    return str(a).replace(":", "-")


def render_template(template: str, fields: dict) -> str:
    found = set(re.findall(r"\{([a-z_]+)\}", template))
    unknown = found - set(PLACEHOLDERS)
    if unknown:
        raise ConfigError(
            "template uses unknown placeholder(s): %s; allowed: %s"
            % (", ".join(sorted(unknown)), ", ".join(PLACEHOLDERS))
        )
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", str(value))
    leftover = sorted(set(re.findall(r"\{([a-z_]+)\}", out)))
    if leftover:
        raise ConfigError("template placeholder(s) have no value: %s" % ", ".join(leftover))
    return out


def validate_hostname(host: str, allow_leading_dash: bool = False) -> str:
    host = host.strip().rstrip(".")
    if not host:
        raise ValueError("empty hostname")
    if len(host) > 253:
        raise ValueError("hostname is %d chars (max 253)" % len(host))
    for label in host.split("."):
        if not label:
            raise ValueError("empty label in %r" % host)
        if len(label) > 63:
            raise ValueError("label %s... is longer than 63 chars" % label[:20])
        if label.startswith("-") and not (allow_leading_dash and len(label) > 1):
            raise ValueError("label %r must not start with '-'" % label)
        if label.endswith("-"):
            raise ValueError("label %r must not end with '-'" % label)
        if _LABEL_RE.search(label.lower()):
            raise ValueError("label %r contains characters outside [a-z0-9-]" % label)
    return host


def build_domain(domain_cfg, vmid: int, name: str, ip: str) -> str:
    """Build the sslip.io hostname for a guest (explicit overrides are handled by the caller)."""
    suffix = (domain_cfg.suffix or "sslip.io").strip(".")
    is_v6 = ipaddress.ip_address(ip).version == 6

    if domain_cfg.mode == "proxy":
        if not any(
            ph in domain_cfg.template
            for ph in ("{public_ip_dashed}", "{public_ip}", "{ip_dashed}")
        ):
            raise ConfigError(
                "[domain].template in proxy mode must embed an IP ({public_ip_dashed}, "
                "{public_ip} or {ip_dashed}) or the sslip.io name will not resolve"
            )
        if not domain_cfg.public_ip:
            raise ConfigError("[domain].public_ip is required in proxy mode")
        fields = {
            "name": slugify(name, domain_cfg.name_max_len),
            "vmid": vmid,
            "public_ip": domain_cfg.public_ip or "",
            "public_ip_dashed": ip_dashed(domain_cfg.public_ip) if domain_cfg.public_ip else "",
            "ip_dashed": ip_dashed(ip),
            "suffix": suffix,
        }
        host = render_template(domain_cfg.template, fields)
        if not host.endswith("." + suffix):
            host = suffix if host == "" else f"{host}.{suffix}"
    elif domain_cfg.mode == "direct":
        labels = ip_dashed(ip)
        if domain_cfg.include_label:
            labels = f"{slugify(name, domain_cfg.name_max_len)}-{vmid}.{labels}"
        host = f"{labels}.{suffix}"
    else:
        raise ConfigError("domain.mode must be 'proxy' or 'direct', got %r" % domain_cfg.mode)

    # NOTE: no multi-dash collapse here — it would corrupt IPv6 labels like 2001-db8--1
    host = host.strip(".")
    return validate_hostname(host, allow_leading_dash=is_v6)
