#!/usr/bin/env python3
"""Strip the APIKEY.FUN sponsor "Quick Start" page from a CLIProxyAPI
Management Center bundle and give it war's red panel icon.

Usage: python3 panel-patch.py IN.html OUT.html

Compose mounts static/management.html (pinned with
disable-auto-update-panel: true). To take a newer panel: fetch the release's
management.html as management.html.orig, rerun this, recreate the container.
Each substitution must match exactly once, so a changed bundle fails loudly
instead of being half-patched.
"""
import base64
import sys

# War-red control-panel icon, matching the other pages on war.
PANEL_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <!-- CLIProxyAPI management panel on war: a control knob and sliders, in war's red -->
  <rect width="64" height="64" rx="14" fill="#14171c"/>
  <rect x="9" y="11" width="46" height="42" rx="6" fill="#262d38" stroke="#4a5566" stroke-width="2"/>
  <g stroke="#4a5566" stroke-width="3" stroke-linecap="round"><path d="M18 22h28M18 32h28M18 42h28"/></g>
  <g fill="#e5484d"><circle cx="38" cy="22" r="4.5"/><circle cx="25" cy="32" r="4.5"/><circle cx="33" cy="42" r="4.5"/></g>
</svg>"""
PANEL_ICON_URI = "data:image/svg+xml;base64," + base64.b64encode(PANEL_ICON.encode()).decode()

SUBS = [
    # Replace the stock Vite favicon; the old link stays in place, inert.
    ('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg',
     f'<link rel="icon" type="image/svg+xml" href="{PANEL_ICON_URI}">'
     '<link rel="x-upstream-icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg'),
    # Quick Start nav item, in whichever sidebar group it lands
    (",...fe?[]:[pe]]}", "]}"),
    (",...fe?[pe]:[]]}", "]}"),
    # /quick-start route -> dashboard
    ("{path:`/quick-start`,element:(0,H.jsx)(fD,{fixedBrand:`apikeyFun`})}",
     "{path:`/quick-start`,element:(0,H.jsx)(ti,{to:`/`,replace:!0})}"),
    # Quota windows timeline: upstream tiles fixed-length windows backwards and
    # forwards from the current reset time, which invents history (real windows
    # only open on first use after the last one expires). Draw the window the
    # provider reports now plus the windows cliproxy-dash observed, fetched from
    # /v8/management/quota-history (routed to cliproxy-dash by the edge). It
    # uses the bare axios instance so a failed fetch can't trigger the panel's
    # 401 logout handler.
    ("function cP(",
     "var QuotaHist__={data:null,at:0,busy:!1};"
     "function quotaHistLoad__(){let e=Date.now();if(QuotaHist__.busy||e-QuotaHist__.at<6e4||!G.apiBase)return;"
     "QuotaHist__.busy=!0;ep.get(`${G.apiBase}/quota-history`,{params:{since:Math.floor(e/1e3-86400*60)},"
     "headers:{Authorization:`Bearer ${G.managementKey}`},timeout:15e3})"
     ".then(e=>{QuotaHist__.data=e.data}).catch(()=>{}).finally(()=>{QuotaHist__.busy=!1,QuotaHist__.at=Date.now()})}"
     "function quotaHistWindows__(e,a,t,n){quotaHistLoad__();"
     "let r=[{startMs:e.anchorMs-a,endMs:e.anchorMs}],"
     "i=QuotaHist__.data?.accounts?.[e.authIndex]?.[a<=18e6?`5h`:`7d`]??[];"
     "for(let s of i){let o=s.start*1e3,c=s.end*1e3;"
     "c>t&&o<n&&!(o<r[0].endMs-6e4&&c>r[0].startMs+6e4)&&"
     "r.push({startMs:o,endMs:c,peak:s.peak,source:s.source,uncertainty:s.uncertainty})}"
     "return r}"
     "function cP("),
    ("iP(e.anchorMs,a*w_,t,n)", "quotaHistWindows__(e,a*w_,t,n)"),
    ("remaining:a===`live`&&t.endMs===e.anchorMs?e.remaining:null",
     "remaining:a===`live`&&t.endMs===e.anchorMs?e.remaining:"
     "t.peak==null?null:Math.max(0,Math.round(100-t.peak)),"
     "source:t.source,uncertainty:t.uncertainty"),
    ("${t.remaining===null?``:`\\n${t.remaining}% remaining`}`",
     "${t.remaining===null?``:`\\n${t.remaining}% remaining`}"
     "${t.source?`\\nrecorded: ${t.source===`reset_time`?`provider reset time`:"
     "t.source===`reset_drop`?`reset seen ±${Math.max(1,Math.round(t.uncertainty/60))} min`:"
     "`opened by first request`}`:``}`"),
    # Quota cards: load every provider's quota on its own (upstream does this
    # only for Devin) and show the loading skeleton instead of a click-to-load
    # button. The reads are served from cliproxy-dash's cache via the edge.
    ("if(e!==`devin`)return!1;let n=JSON.stringify([i,a[t.name]??0,t.name,t.authIndex]);"
     "return r.current.has(n)?!1:(r.current.add(n),jm.getState().devinQuota[Om(t)]?.status!==`loading`)",
     "let s=wA[e];if(!s)return!1;let n=JSON.stringify([i,a[t.name]??0,t.name,t.authIndex]);"
     "return r.current.has(n)?!1:(r.current.add(n),s.storeSelector(jm.getState())[Om(t)]?.status!==`loading`)"),
    ("g===`idle`?(0,H.jsxs)(`button`,{type:`button`,className:$N.idleBody,onClick:s,disabled:!i,"
     "children:[(0,H.jsx)(ws,{size:15,\"aria-hidden\":`true`,className:$N.idleGlyph}),"
     "(0,H.jsx)(`span`,{className:$N.idleHint,children:l(`${u.i18nPrefix}.idle`)})]}):_&&!f?",
     "g===`idle`||_&&!f?"),
    # Lanes need the credential's auth_index to match recorded windows.
    ("o={name:t,displayName:n,provider:r,anchorMs:null,",
     "o={name:t,displayName:n,provider:r,authIndex:e.authIndex,anchorMs:null,"),
    ("name:Om(e.file),displayName:e.type===`devin`?km(e.file):n(e.file.name),"
     "provider:e.type,quota:t(e)",
     "name:Om(e.file),displayName:e.type===`devin`?km(e.file):n(e.file.name),"
     "provider:e.type,quota:t(e),authIndex:e.file.auth_index??e.file.authIndex"),
    # Nothing upcoming is drawn: the next window only opens on first use.
    ("(0,H.jsxs)(`span`,{className:mP.legendItem,children:[(0,H.jsx)(`span`,"
     "{className:`${mP.swatch} ${mP.swatchNext}`}),s(`quota_management."
     "windows_legend_upcoming`,{defaultValue:`upcoming`})]}),", ""),
    ("windows_legend_elapsed:`elapsed`", "windows_legend_elapsed:`past (recorded)`"),
    ("windows_note_weekly:`Each bar is one full quota window, drawn from when "
     "it opened to when it resets. Lanes ending together compete for the same "
     "days.`",
     "windows_note_weekly:`Each bar is a window that actually happened, from "
     "when it opened to when it reset, as recorded by cliproxy-dash; the "
     "current one is the provider's own. Gaps are time with no window open.`"),
    ("windows_note_session:`Each bar is one 5-hour window. Only credentials "
     "with a window counting down can be projected; the rest stay empty rather "
     "than invented.`",
     "windows_note_session:`Each bar is a 5-hour window that actually "
     "happened, as recorded by cliproxy-dash; the current one is the "
     "provider's own. Gaps are time with no window open. Hover a bar for its "
     "source and precision.`"),
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
