#!/usr/bin/env bash
# Install the Scale hub as a systemd service (runs on boot).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/scale.service"
UNIT_DST="/etc/systemd/system/scale.service"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "Missing $UNIT_SRC"
  exit 1
fi

echo "Installing $UNIT_DST"
sudo cp "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable scale.service
sudo systemctl restart scale.service
sudo systemctl --no-pager --full status scale.service || true

echo
echo "Done. Useful commands:"
echo "  sudo systemctl status scale"
echo "  sudo systemctl restart scale"
echo "  journalctl -u scale -f"
