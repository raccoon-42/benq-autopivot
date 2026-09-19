#!/bin/sh
# Install the daemon as a systemd user service.
set -e
lib="$HOME/.local/lib/benq-autopivot"
mkdir -p "$lib" "$HOME/.config/systemd/user"
install -m 755 autopivot.py "$lib/autopivot.py"
install -m 644 benq-autopivot.service "$HOME/.config/systemd/user/benq-autopivot.service"
systemctl --user daemon-reload
systemctl --user enable --now benq-autopivot.service
systemctl --user --no-pager status benq-autopivot.service || true
