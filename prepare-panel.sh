#!/usr/bin/env bash
# Download the pinned upstream panel and apply our local patch.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --location --silent --show-error \
  https://github.com/router-for-me/Cli-Proxy-API-Management-Center/releases/download/v1.26.5/management.html \
  --output "$work/management.html"
printf '%s  %s\n' 57558cd023475674634a114824b014d3e5ac161f6f3791e3d86b6adab962a700 "$work/management.html" | sha256sum --check --status
python3 panel-patch.py "$work/management.html" "$work/patched.html"
mkdir -p static
cp "$work/management.html" static/management.html.orig
cp "$work/patched.html" static/management.html
