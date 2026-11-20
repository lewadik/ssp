"""Proxmox VE integration: REST API client (token or ticket auth) and pvesh/qm/pct CLI fallback,
plus guest IP discovery (QEMU guest agent, LXC interfaces endpoint, CT config netX parsing)."""

from __future__ import annotations

import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .util import ProxmoxError, Runner


# --------------------------------------------------------------------------- #
# IP discovery helpers (pure functions, unit-tested)
# --------------------------------------------------------------------------- #

def _usable(ip: str, family: str | None) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_link_local or a.is_unspecified:
        return False
    if family == "ipv4" and a.version != 4:
        return False
    if family == "ipv6" and a.version != 6:
        return False
    return True


def pick_ip(candidates, prefer: str | None = None, family: str | None = None):
    """candidates: list of (iface_name, [(ip, family), ...]) — prefer named iface, then first usable."""
    for name, pairs in candidates:
        if prefer and name == prefer:
            for ip, fam in pairs:
                if _usable(ip, family):
                    return ip
    for name, pairs in candidates:
        for ip, fam in pairs:
            if _usable(ip, family):
                return ip
    return None


def normalize_agent_interfaces(raw) -> list:
    """QEMU guest agent `network-get-interfaces` payload -> [(name, [(ip, family), ...])]."""
    out = []
    for ifc in raw or []:
        name = ifc.get("name") or ifc.get("ifname") or ""
        pairs = []
        for a in ifc.get("ip-addresses") or []:
            ip = a.get("ip-address")
            fam = a.get("ip-address-type")
            if ip:
                pairs.append((ip, fam or ("ipv6" if ":" in ip else "ipv4")))
        out.append((name, pairs))
    return out


def normalize_ip_j(raw) -> list:
    """`ip -j addr` output -> [(ifname, [(local, family), ...])]."""
    out = []
    for ifc in raw or []:
        name = ifc.get("ifname") or ""
        pairs = []
        for a in ifc.get("addr_info") or []:
            ip = a.get("local")
            fam = {"inet": "ipv4", "inet6": "ipv6"}.get(a.get("family"))
            if ip:
                pairs.append((ip, fam or ("ipv6" if ":" in ip else "ipv4")))
        out.append((name, pairs))
    return out


def normalize_lxc_interfaces(raw) -> list:
    """GET /nodes/{node}/lxc/{vmid}/interfaces payload -> [(name, [(ip, family), ...])]."""
    out = []
    for ifc in raw or []:
        name = ifc.get("name") or ""
        pairs = []
        for fam, key in (("ipv4", "ipv4"), ("ipv6", "ipv6")):
            for a in ifc.get(key) or []:
                ip = a.get("address")
                if ip:
                    pairs.append((ip, fam))
        out.append((name, pairs))
    return out


_NET_IP_RE = re.compile(r"(?:^|,)ip=([^,\s]+)")


def parse_ct_config_ip(net_strings: dict):
    """Parse LXC config `netX: ... ip=ADDR[/prefix]` entries; returns first static IP or None."""
    for key in sorted(net_strings or {}):
        value = net_strings[key] or ""
        m = _NET_IP_RE.search(value)
        if not m:
            continue
        ip = m.group(1)
        if ip in ("dhcp", "auto", "manual"):
            continue
        ip = ip.split("/")[0]
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        return ip
    return None


def filter_resources(raw) -> list:
    """Keep VMs and containers from /cluster/resources."""
    return [r for r in raw or [] if r.get("type") in ("vm", "lxc") and "vmid" in r]


# --------------------------------------------------------------------------- #
# REST API client
# --------------------------------------------------------------------------- #

def build_request(base_url: str, method: str, path: str, params: dict | None):
    """Returns (url, headers, body) for a PVE API call (path starts with '/')."""
    base = base_url.rstrip("/")
    headers = {"Accept": "application/json"}
    url = base + "/api2/json" + path
    body = None
    if method in ("POST", "PUT"):
        body = urllib.parse.urlencode(params or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    return url, headers, body


class ProxmoxAPI:
    """PVE REST API client. Token auth preferred; password auth uses the ticket flow."""

    def __init__(self, url, token_id=None, token_secret=None, password=None,
                 user="root@pam", verify_ssl=False, timeout=10, urlopen=None):
        self.base = url.rstrip("/")
        self.token_id = token_id
        self.token_secret = token_secret
        self.password = password
        self.user = user
        self.timeout = timeout
        self._ssl_ctx = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()
        self._urlopen = urlopen or urllib.request.urlopen
        self._ticket: str | None = None
        self._csrf: str | None = None

    # -- auth ---------------------------------------------------------------
    def _auth_headers(self) -> dict:
        if self.token_id and self.token_secret:
            return {"Authorization": "PVEAPIToken=%s=%s" % (self.token_id, self.token_secret)}
        if self.password:
            self._login()
            return {"Cookie": "PVEAuthCookie=%s" % self._ticket,
                    "CSRFPreventionToken": self._csrf or ""}
        raise ProxmoxError("no Proxmox credentials configured: set token_id/token_secret or password")

    def _login(self):
        if self._ticket:
            return
        url, headers, body = build_request(self.base, "POST", "/access/ticket",
                                           {"username": self.user, "password": self.password})
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                data = json.load(resp)["data"]
        except urllib.error.HTTPError as e:
            raise ProxmoxError("login failed: HTTP %s %s" % (e.code, e.reason)) from e
        except Exception as e:  # noqa: BLE001
            raise ProxmoxError("login failed: %s" % e) from e
        if not data or not data.get("ticket"):
            raise ProxmoxError("login failed: no ticket in response")
        self._ticket = data["ticket"]
        self._csrf = data.get("CSRFPreventionToken")

    # -- requests -----------------------------------------------------------
    def request(self, method: str, path: str, params: dict | None = None):
        url, headers, body = build_request(self.base, method, path, params)
        headers.update(self._auth_headers())
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise ProxmoxError("%s %s -> HTTP %s %s %s" % (method, path, e.code, e.reason, detail)) from e
        except Exception as e:  # noqa: BLE001
            raise ProxmoxError("%s %s -> %s" % (method, path, e)) from e
        return payload.get("data") if isinstance(payload, dict) else payload

    # -- high level ---------------------------------------------------------
    def list_resources(self) -> list:
        return filter_resources(self.request("GET", "/cluster/resources"))

    def guest_ip(self, res: dict, prefer: str | None = None, family: str | None = None):
        vmid, node, rtype = res["vmid"], res["node"], res["type"]
        if rtype == "lxc":
            ip = self._lxc_ip(vmid, node, prefer, family)
        else:
            ip = self._vm_ip(vmid, node, prefer, family)
        if not ip:
            raise ProxmoxError("no usable IP reported by guest %s" % vmid)
        return ip

    def _vm_ip(self, vmid, node, prefer, family):
        try:
            data = self.request("POST", "/nodes/%s/qemu/%s/agent/network-get-interfaces" % (node, vmid))
        except ProxmoxError as e:
            raise ProxmoxError(
                "qemu guest agent query failed for VM %s (%s); start the VM, install "
                "qemu-guest-agent, or pin the IP in [targets.%s].ip" % (vmid, e, vmid)
            ) from e
        raw = data.get("result") if isinstance(data, dict) else data
        return pick_ip(normalize_agent_interfaces(raw), prefer=prefer, family=family)

    def _lxc_ip(self, vmid, node, prefer, family):
        try:
            data = self.request("GET", "/nodes/%s/lxc/%s/interfaces" % (node, vmid))
            ip = pick_ip(normalize_lxc_interfaces(data), prefer=prefer, family=family)
            if ip:
                return ip
        except ProxmoxError:
            pass  # older PVE: fall through to config parsing
        cfg = self.request("GET", "/nodes/%s/lxc/%s/config" % (node, vmid)) or {}
        nets = {k: v for k, v in cfg.items() if k.startswith("net")}
        ip = parse_ct_config_ip(nets)
        if not ip:
            raise ProxmoxError(
                "no static IP in CT %s config and no interfaces reported; set ip=... on a netX "
                "device or pin the IP in [targets.%s].ip" % (vmid, vmid)
            )
        return ip


# --------------------------------------------------------------------------- #
# CLI fallback (runs on the PVE host itself)
# --------------------------------------------------------------------------- #

class ProxmoxCLI:
    def __init__(self, runner: Runner | None = None, node: str | None = None):
        self.runner = runner or Runner()
        self.node = node

    def list_resources(self) -> list:
        r = self.runner.run(["pvesh", "get", "/cluster/resources", "--output-format", "json"])
        if r.rc != 0:
            raise ProxmoxError("pvesh failed: %s" % r.combined)
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ProxmoxError("pvesh returned invalid JSON: %s" % e) from e
        if isinstance(data, dict):
            data = data.get("data") or []
        return filter_resources(data)

    def guest_ip(self, res: dict, prefer: str | None = None, family: str | None = None):
        vmid, rtype = res["vmid"], res["type"]
        if rtype == "lxc":
            return self._lxc_ip(vmid, prefer, family)
        return self._vm_ip(vmid, prefer, family)

    def _vm_ip(self, vmid, prefer, family):
        r = self.runner.run(["qm", "guest", str(vmid), "network-get-interfaces"])
        if r.rc != 0:
            raise ProxmoxError(
                "qemu guest agent query failed for VM %s (%s); start the VM, install "
                "qemu-guest-agent, or pin the IP in [targets.%s].ip" % (vmid, r.combined, vmid)
            )
        try:
            raw = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ProxmoxError("qemu agent returned invalid JSON: %s" % e) from e
        if isinstance(raw, dict):
            raw = raw.get("result") or []
        ip = pick_ip(normalize_agent_interfaces(raw), prefer=prefer, family=family)
        if not ip:
            raise ProxmoxError("no usable IP reported by guest %s" % vmid)
        return ip

    def _lxc_ip(self, vmid, prefer, family):
        r = self.runner.run(["pct", "config", str(vmid)])
        if r.rc == 0:
            nets = {}
            for line in r.stdout.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    key, value = key.strip(), value.strip()
                    if key.startswith("net"):
                        nets[key] = value
            ip = parse_ct_config_ip(nets)
            if ip:
                return ip
        r = self.runner.run(["pct", "exec", str(vmid), "--", "ip", "-j", "addr"])
        if r.rc == 0:
            try:
                ip = pick_ip(normalize_ip_j(json.loads(r.stdout)), prefer=prefer, family=family)
                if ip:
                    return ip
            except json.JSONDecodeError:
                pass
        raise ProxmoxError(
            "no static IP in CT %s config and `ip -j addr` failed; set ip=... on a netX device "
            "or pin the IP in [targets.%s].ip" % (vmid, vmid)
        )


def from_config(cfg, runner: Runner | None = None):
    """Build the configured Proxmox provider."""
    px = cfg.proxmox
    if px.mode == "cli":
        return ProxmoxCLI(runner=runner, node=px.node)
    return ProxmoxAPI(
        url=px.url, token_id=px.token_id, token_secret=px.token_secret, password=px.password,
        user=px.user, verify_ssl=px.verify_ssl, timeout=px.timeout,
    )
