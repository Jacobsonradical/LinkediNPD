#!/bin/sh
set -e

# The VNC password guards the one-time login window. It is generated once and
# kept in the state volume; regenerating it every boot would mean digging a new
# password out of the logs on every restart.
if [ ! -f /state/vncpasswd ]; then
    PASS=$(python -c "import secrets; print(secrets.token_urlsafe(9))")
    x11vnc -storepasswd "$PASS" /state/vncpasswd >/dev/null 2>&1
    chmod 600 /state/vncpasswd
    printf '%s' "$PASS" > /state/vncpass.txt
    chmod 600 /state/vncpass.txt
fi

# The dashboard token normally comes from app/main.py, but the noVNC pages
# below need it before the app is up, so make sure it exists here. main.py
# reads whatever is in this file rather than generating its own.
if [ ! -s /state/dashboard-token ]; then
    python -c "import secrets; print(secrets.token_urlsafe(32), end='')" > /state/dashboard-token
    chmod 600 /state/dashboard-token
fi

DASHBOARD_ORIGIN="http://127.0.0.1:${DASHBOARD_PORT:-8765}"
DASHBOARD_URL="$DASHBOARD_ORIGIN/#token=$(cat /state/dashboard-token)"

# noVNC takes the password as a query parameter and connects on its own, so
# there is no second login to type. The password still guards the socket: a
# WebSocket is not covered by the same-origin policy, so without it any page
# you visited could drive this display.
NOVNC_URL="http://127.0.0.1:${NOVNC_PORT:-6080}/vnc.html?autoconnect=true&resize=scale&password=$(cat /state/vncpass.txt)"

# noVNC's stock pages have no idea the dashboard exists, and people land here
# when they get lost. Build a web root that is the vendor files plus a landing
# page at / and a "Dashboard" link injected into vnc.html itself.
WEB=/tmp/novnc-web
rm -rf "$WEB" && mkdir -p "$WEB"
for f in /usr/share/novnc/*; do
    ln -s "$f" "$WEB/$(basename "$f")"
done
rm -f "$WEB/vnc.html"
cp /app/app/web/static/icon.svg "$WEB/icon.svg"
# Python rather than sed for both files: the URLs contain '#' and '&', which
# are special to sed, and the first version of this crashed the container on
# exactly that.
DASHBOARD_ORIGIN="$DASHBOARD_ORIGIN" DASHBOARD_URL="$DASHBOARD_URL" NOVNC_URL="$NOVNC_URL" \
python - <<'PYEOF'
import json
import os
import re
web = "/tmp/novnc-web"
dash = os.environ["DASHBOARD_URL"]

# The stock noVNC page, plus a link back to the dashboard in a new tab.
link = ('<a href="%s" target="_blank" rel="noopener" style="position:fixed;top:8px;'
        'right:10px;z-index:9999;background:#0a66c2;color:#fff;font:600 13px/1 '
        '-apple-system,Segoe UI,Roboto,Ubuntu,sans-serif;padding:8px 12px;'
        'border-radius:7px;text-decoration:none;opacity:.92">Dashboard &#8599;</a>' % dash)
# Our icon in place of the noVNC wordmark in the side bar and on the
# connecting screen; everything functional in that bar is left alone.
# A bare /vnc.html (typed by hand, or a stale bookmark) lands on noVNC's own
# "Connect" dialog. Send it to the auto-connecting URL instead.
novnc = os.environ["NOVNC_URL"]
autoconnect = ('<script>if(!/[?&]autoconnect=/.test(location.search)){location.replace(%s)}</script>'
               % json.dumps(novnc))
brand = ('<style>.noVNC_logo{display:none!important}'
         '#noVNC_control_bar .lnpd{width:30px;height:30px;display:block;margin:10px auto 2px}</style>'
         '<script>addEventListener("DOMContentLoaded",()=>{const b=document.getElementById("noVNC_control_bar");'
         'if(b){const i=document.createElement("img");i.src="/icon.svg";i.className="lnpd";i.alt="LinkediNPD";'
         'b.insertBefore(i,b.firstChild)}});</script>')
html = open("/usr/share/novnc/vnc.html", encoding="utf-8").read()
html = html.replace("<title>noVNC</title>", "<title>LinkediNPD login window</title>", 1)
# noVNC declares a dozen sized PNG icons; browsers pick those over a single
# SVG added alongside, so they have to go.
html = re.sub(r'<link[^>]*rel="(?:icon|apple-touch-icon)"[^>]*>\s*', "", html)
html = html.replace("</head>", '<link rel="icon" type="image/svg+xml" href="/icon.svg">' + autoconnect + brand + "</head>", 1)
open(f"{web}/vnc.html", "w", encoding="utf-8").write(html.replace("</body>", link + "</body>", 1))

# The landing page at /, pointing both ways.
page = open("/app/docker/novnc-landing.html", encoding="utf-8").read()
for key in ("DASHBOARD_ORIGIN", "DASHBOARD_URL", "NOVNC_URL"):
    page = page.replace(f"__{key}__", os.environ[key])
open(f"{web}/index.html", "w", encoding="utf-8").write(page)
PYEOF

echo "===================================================================="
echo "  Dashboard (counters, log, settings, controls):"
echo "  $DASHBOARD_URL"
echo ""
echo "  LinkedIn login window (one click, no password needed):"
echo "  $NOVNC_URL"
echo "===================================================================="

exec supervisord -c /app/docker/supervisord.conf
