#!/usr/bin/env bash
# Installiert das WoL-Relais auf dem Heimserver.
#
#   sudo bash install.sh <IP-des-PCs-im-Heimnetz> [MAC-Adresse]
#
# Der PC sollte dabei eingeschaltet sein: Dann findet das Skript seine MAC-Adresse
# selbst. Ist er aus, gib die MAC als zweites Argument an (Windows: getmac /v).
#
# Optional davor setzen:
#   WOL_NAME="Gaming-PC"  Anzeigename auf der Webseite   (Standard: PC)
#   WOL_PORT=8090         Port der Webseite              (Standard: 8090)
#   PROBELAUF=1           nur pruefen und anzeigen, nichts installieren
set -euo pipefail

PORT="${WOL_PORT:-8090}"
NAME="${WOL_NAME:-PC}"
PROBELAUF="${PROBELAUF:-0}"
APP_DIR=/opt/wol-relais
ENV_FILE=/etc/wol-relais.env
UNIT_FILE=/etc/systemd/system/wol-relais.service
HELPER=/usr/local/bin/pc-wecken
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fehler() { echo "FEHLER: $*" >&2; exit 1; }
info()   { echo "  $*"; }

# --- Voraussetzungen ---------------------------------------------------------

if [ "$PROBELAUF" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  fehler "Bitte mit sudo starten: sudo bash $0 <IP-des-PCs> [MAC]"
fi
[ $# -ge 1 ] || fehler "IP-Adresse des PCs fehlt. Aufruf: sudo bash $0 <IP-des-PCs> [MAC]
         Die IP zeigt Windows mit 'ipconfig' unter IPv4-Adresse (meist 192.168.x.x)."
command -v python3 >/dev/null || fehler "python3 fehlt (sudo apt install python3)"
command -v ip      >/dev/null || fehler "Das Programm 'ip' fehlt (sudo apt install iproute2)"
[ -f "$SRC_DIR/wol_server.py" ] || fehler "wol_server.py nicht gefunden. Bitte install.sh aus dem Ordner wol-relais starten."

PC_IP="$1"
MAC="${2:-}"

python3 - "$PC_IP" <<'PY' || exit 1
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    sys.exit(f"FEHLER: '{sys.argv[1]}' ist keine gueltige IPv4-Adresse.")
if ip in ipaddress.ip_network("100.64.0.0/10"):
    sys.exit("FEHLER: Das ist eine Tailscale-Adresse. Gebraucht wird die Heimnetz-IP des PCs\n"
             "       (Windows: ipconfig -> IPv4-Adresse des LAN-Adapters, meist 192.168.x.x).")
if not ip.is_private:
    sys.exit(f"FEHLER: {ip} ist keine Heimnetz-Adresse.")
PY

echo "== Netzwerk erkennen"

ROUTE="$(ip -4 route get "$PC_IP" 2>/dev/null | head -1)" || true
[ -n "$ROUTE" ] || fehler "Keine Route zu $PC_IP gefunden."
IFACE="$(sed -n 's/.* dev \([^ ]*\).*/\1/p' <<<"$ROUTE")"
SERVER_IP="$(sed -n 's/.* src \([0-9.]*\).*/\1/p' <<<"$ROUTE")"
if grep -q ' via ' <<<"$ROUTE"; then
  fehler "$PC_IP liegt nicht im selben Netz wie der Server (Route: $ROUTE).
         Ein WoL-Broadcast kommt dort nicht an. Stimmt die IP?"
fi
case "$IFACE" in
  tailscale*|docker*|br-*|veth*|lo)
    fehler "$PC_IP wird ueber '$IFACE' erreicht, nicht ueber das Heimnetz. Stimmt die IP?";;
esac

ADDR_LINE="$(ip -4 -o addr show dev "$IFACE" | head -1)"
CIDR="$(sed -n 's/.* inet \([0-9.]*\/[0-9]*\).*/\1/p' <<<"$ADDR_LINE")"
BROADCAST="$(sed -n 's/.* brd \([0-9.]*\).*/\1/p' <<<"$ADDR_LINE")"
[ -n "$BROADCAST" ] || BROADCAST=255.255.255.255
LAN_NET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$CIDR")"

info "Schnittstelle:   $IFACE ($CIDR)"
info "Broadcast:       $BROADCAST"

if [ -z "$MAC" ]; then
  # Ein Ping (oder Verbindungsversuch) loest eine ARP-Anfrage aus. Darauf antwortet
  # Windows auch dann, wenn die Firewall den Ping selbst blockiert.
  ping -c 2 -W 1 "$PC_IP" >/dev/null 2>&1 || true
  timeout 2 bash -c ": </dev/tcp/$PC_IP/445" >/dev/null 2>&1 || true
  MAC="$(ip neigh show "$PC_IP" dev "$IFACE" | sed -n 's/.* lladdr \([0-9a-fA-F:]*\).*/\1/p' | head -1)"
  [ -n "$MAC" ] || fehler "MAC-Adresse von $PC_IP nicht gefunden. Ist der PC an und per Kabel im Heimnetz?
         Sonst die MAC als zweites Argument angeben: sudo bash $0 $PC_IP AA-BB-CC-DD-EE-FF
         (Windows: getmac /v zeigt sie unter 'Physische Adresse' der Ethernet-Verbindung)."
  info "MAC (erkannt):   $MAC"
else
  info "MAC (angegeben): $MAC"
fi
MAC="$(python3 -c '
import re, sys
t = sys.argv[1].strip()
d = re.sub(r"[^0-9A-Fa-f]", "", t)
if not re.fullmatch(r"[0-9A-Fa-f:.\- ]+", t) or len(d) != 12:
    sys.exit("FEHLER: ungueltige MAC-Adresse: " + t)
print(":".join(d[i:i+2] for i in range(0, 12, 2)).lower())
' "$MAC")"

TS_IP=""; TS_NAME=""
if command -v tailscale >/dev/null; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  TS_NAME="$(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])
except Exception:
    pass' || true)"
fi

if [ "$PROBELAUF" = 1 ]; then
  echo
  echo "== Probelauf: so wuerde installiert (nichts geaendert)"
  info "WOL_NAME=$NAME  WOL_MAC=$MAC  WOL_HOST=$PC_IP  WOL_BROADCAST=$BROADCAST  WOL_PORT=$PORT"
  info "Tailscale:       ${TS_IP:-nicht gefunden} ${TS_NAME:+($TS_NAME)}"
  exit 0
fi

command -v systemctl >/dev/null || fehler "systemd wird benoetigt."
command -v ping >/dev/null || echo "  Hinweis: 'ping' fehlt (sudo apt install iputils-ping). Die An/Aus-Anzeige nutzt dann nur TCP-Ports."

echo "== Installieren"

if systemctl is-active --quiet wol-relais 2>/dev/null; then
  systemctl stop wol-relais
fi
if command -v ss >/dev/null && [ -n "$(ss -ltnH "sport = :$PORT" 2>/dev/null)" ]; then
  fehler "Port $PORT ist schon belegt. Anderen Port waehlen: sudo WOL_PORT=8091 bash $0 $*"
fi

install -d -m 0755 "$APP_DIR"
install -m 0644 "$SRC_DIR/wol_server.py" "$APP_DIR/wol_server.py"
install -m 0644 "$SRC_DIR/wol-relais.service" "$UNIT_FILE"

cat > "$ENV_FILE" <<EOF
# Einstellungen fuer das WoL-Relais. Nach Aenderungen: sudo systemctl restart wol-relais
WOL_NAME="$NAME"
WOL_MAC=$MAC
WOL_HOST=$PC_IP
WOL_BROADCAST=$BROADCAST
WOL_LISTEN=0.0.0.0
WOL_PORT=$PORT
EOF
chmod 0644 "$ENV_FILE"

cat > "$HELPER" <<'EOF'
#!/bin/sh
# pc-wecken          PC aufwecken
# pc-wecken status   anzeigen, ob der PC an ist
set -a
. /etc/wol-relais.env
set +a
exec /usr/bin/python3 /opt/wol-relais/wol_server.py "${1:-wake}"
EOF
chmod 0755 "$HELPER"
info "Programm:        $APP_DIR/wol_server.py"
info "Einstellungen:   $ENV_FILE"
info "Befehl:          pc-wecken  (bzw. pc-wecken status)"

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow from "$LAN_NET" to any port "$PORT" proto tcp comment "WoL-Relais (Heimnetz)" >/dev/null
  ufw allow in on tailscale0 to any port "$PORT" proto tcp comment "WoL-Relais (Tailscale)" >/dev/null
  info "Firewall (ufw):  Port $PORT fuer $LAN_NET und Tailscale freigegeben"
fi

systemctl daemon-reload
systemctl enable --now wol-relais >/dev/null 2>&1
sleep 1

echo "== Test"
if STATUS="$(python3 - "$PORT" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/status", timeout=8) as r:
    d = json.load(r)
print({True: "PC ist gerade AN", False: "PC ist gerade AUS", None: "Status unbekannt"}[d["online"]])
PY
)"; then
  info "Dienst laeuft.   $STATUS"
else
  echo "  Der Dienst antwortet nicht. Log ansehen mit: journalctl -u wol-relais -n 30"
  systemctl --no-pager status wol-relais | head -15 || true
  exit 1
fi

echo
echo "Fertig. Aufrufen im Browser:"
info "zu Hause:     http://${SERVER_IP}:${PORT}"
[ -n "$TS_IP" ]   && info "unterwegs:    http://${TS_IP}:${PORT}   (Tailscale auf dem Handy an)"
[ -n "$TS_NAME" ] && info "oder kurz:    http://${TS_NAME}:${PORT}   (wenn MagicDNS an ist)"
echo
echo "Am besten die Tailscale-Adresse auf dem Handy als Verknuepfung auf den Startbildschirm legen."
echo "Die funktioniert zu Hause und unterwegs."
