#!/usr/bin/env python3
"""Strip the APIKEY.FUN sponsor "Quick Start" page from a CLIProxyAPI
Management Center bundle.

Usage: python3 panel-patch.py IN.html OUT.html

Compose mounts static/management.html (pinned with
disable-auto-update-panel: true). To take a newer panel: fetch the release's
management.html as management.html.orig, rerun this, recreate the container.
Each substitution must match exactly once, so a changed bundle fails loudly
instead of being half-patched.
"""
import sys

SUBS = [
    # Quick Start nav item, in whichever sidebar group it lands
    (",...fe?[]:[pe]]}", "]}"),
    (",...fe?[pe]:[]]}", "]}"),
    # /quick-start route -> dashboard
    ("{path:`/quick-start`,element:(0,H.jsx)(fD,{fixedBrand:`apikeyFun`})}",
     "{path:`/quick-start`,element:(0,H.jsx)(ti,{to:`/`,replace:!0})}"),
]

src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
for old, new in SUBS:
    n = s.count(old)
    if n != 1:
        sys.exit(f"expected 1 match, found {n}: {old[:60]}")
    s = s.replace(old, new)
open(dst, "w", encoding="utf-8").write(s)
print(f"patched {dst}")
