#!/usr/bin/env bash
# Install/update ssipng on a Proxmox VE host (Debian, Python 3.11+). Run as root.
#   packaging/install.sh             install or update
#   packaging/install.sh --uninstall remove (keeps /etc/ssipng and /var/lib/ssipng)
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
LIBDIR="$PREFIX/lib/ssipng"
BINDIR="$PREFIX/bin"
CONFDIR="/etc/ssipng"
STATEDIR="/var/lib/ssipng"
SERVICE="/etc/systemd/system/ssipng.service"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl disable --now ssipng.service 2>/dev/null || true
  rm -rf "$LIBDIR" "$BINDIR/ssipng" "$SERVICE"
  systemctl daemon-reload
  echo "ssipng removed (config kept in $CONFDIR, state kept in $STATEDIR)"
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || { echo "run as root"; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "python3 >= 3.11 required (PVE 8+ ships 3.11)"; exit 1; }

install -d "$LIBDIR" "$BINDIR" "$CONFDIR" "$STATEDIR"
rm -rf "$LIBDIR/ssipng"
cp -r "$REPO/ssipng" "$LIBDIR/ssipng"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$BINDIR/ssipng" <<WRAPPER
#!/usr/bin/env bash
export PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m ssipng "\$@"
WRAPPER
chmod 0755 "$BINDIR/ssipng"

install -m 0644 "$REPO/packaging/ssipng.service" "$SERVICE"
systemctl daemon-reload

if [[ ! -f "$CONFDIR/config.toml" ]]; then
  install -m 0600 "$REPO/config.example.toml" "$CONFDIR/config.toml"
  echo "starter config written: $CONFDIR/config.toml"
fi

echo
echo "installed. next steps:"
echo "  1. nano $CONFDIR/config.toml   # set token_secret, [domain] public_ip, [web] backend"
echo "  2. pveum user token add root@pam ssipng --privsep 0"
echo "  3. $BINDIR/ssipng sync --dry-run"
echo "  4. $BINDIR/ssipng sync         # apply once"
echo "  5. systemctl enable --now ssipng   # keep assigning newly created guests"
