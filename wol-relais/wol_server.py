#!/usr/bin/env python3
"""WoL-Relais: weckt den PC per Magic Packet, vom Handy aus und auch von unterwegs.

Ein Handy kann das Magic Packet von unterwegs nicht selbst zustellen, denn ein
Broadcast geht weder durchs Internet noch durch Tailscale. Dieser Dienst laeuft
deshalb auf dem Heimserver: Das Handy ruft ihn ueber Tailscale (oder zu Hause
direkt) auf, und der Server schickt das Paket als Broadcast ins Heimnetz.

Nur Python-Standardbibliothek, pip ist nicht noetig.

Einstellungen kommen aus Umgebungsvariablen (install.sh schreibt sie nach
/etc/wol-relais.env):

    WOL_MAC        MAC-Adresse des PCs                      (Pflicht)
    WOL_BROADCAST  Broadcast-Adresse des Heimnetzes         (Standard: 255.255.255.255)
    WOL_HOST       IP des PCs, nur fuer die Statusanzeige   (optional)
    WOL_NAME       Anzeigename                              (Standard: PC)
    WOL_LISTEN     Adresse, auf der der Dienst lauscht      (Standard: 0.0.0.0)
    WOL_PORT       Port                                     (Standard: 8090)

Aufruf:
    wol_server.py serve     Webseite und API starten (Standard)
    wol_server.py wake      Magic Packet einmal senden
    wol_server.py status    ausgeben, ob der PC erreichbar ist

API (fuer Home Assistant, Kurzbefehle usw.):
    GET  /api/status   {"name": "PC", "online": true|false|null, ...}
    POST /api/wake     sendet das Magic Packet, antwortet {"ok": true, ...}
"""

import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Wer den Dienst aufrufen darf: das eigene Heimnetz, Docker-Netze und Tailscale.
# Aus dem Internet ist er ohnehin nicht erreichbar, solange im Router kein Port
# freigegeben ist. Das hier ist die zweite Sicherung.
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8",      # der Server selbst
    "10.0.0.0/8",       # private Heimnetze
    "172.16.0.0/12",    # private Netze, auch Docker (z. B. Home Assistant im Container)
    "192.168.0.0/16",   # private Heimnetze (Fritzbox usw.)
    "100.64.0.0/10",    # Tailscale (IPv4)
    "::1/128",
    "fc00::/7",         # private IPv6-Netze, auch Tailscale (fd7a:115c:a1e0::/48)
    "fe80::/10",
)]

# Ports, an denen ein eingeschalteter PC erkennbar ist, auch wenn er Ping blockiert:
# SMB (Dateifreigabe), RDP (Remotedesktop), RPC, SSH.
PROBE_PORTS = (445, 3389, 135, 22)
STATUS_CACHE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Magic Packet
# ---------------------------------------------------------------------------

def parse_mac(text):
    """'AA-BB-CC-DD-EE-FF', 'aa:bb:...' oder 'aabb.ccdd.eeff' -> 6 Bytes."""
    text = (text or "").strip()
    if not re.fullmatch(r"[0-9A-Fa-f:.\- ]+", text):
        raise ValueError(f"ungueltige MAC-Adresse: {text!r}")
    digits = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(digits) != 12:
        raise ValueError(f"ungueltige MAC-Adresse: {text!r}")
    return bytes.fromhex(digits)


def format_mac(mac_bytes):
    return ":".join(f"{b:02x}" for b in mac_bytes)


def magic_packet(mac_bytes):
    """6 x 0xFF, danach 16 x die MAC-Adresse: das erkennt die Netzwerkkarte."""
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(mac_bytes, broadcast, repeats=3):
    """Schickt das Paket mehrmals an UDP-Port 9 und 7. Doppelt haelt besser:
    UDP hat keine Zustellgarantie, und manche Karten hoeren nur auf einen Port."""
    packet = magic_packet(mac_bytes)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for i in range(repeats):
            for port in (9, 7):
                sock.sendto(packet, (broadcast, port))
            if i < repeats - 1:
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Ist der PC an?
# ---------------------------------------------------------------------------

def _tcp_probe(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return True  # "Verbindung abgelehnt" kann nur ein laufender Rechner sagen
    except OSError:
        return False


def _ping(host, timeout):
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, round(timeout))), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_online(host, timeout=1.5):
    """Ping und ein paar TCP-Ports gleichzeitig; der erste Treffer zaehlt."""
    pool = ThreadPoolExecutor(max_workers=len(PROBE_PORTS) + 1)
    try:
        futures = [pool.submit(_ping, host, timeout)]
        futures += [pool.submit(_tcp_probe, host, port, timeout) for port in PROBE_PORTS]
        for future in as_completed(futures):
            if future.result():
                return True
        return False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, env):
        mac = env.get("WOL_MAC", "")
        if not mac:
            raise SystemExit("WOL_MAC fehlt (MAC-Adresse des PCs, z. B. aa:bb:cc:dd:ee:ff)")
        try:
            self.mac = parse_mac(mac)
        except ValueError as exc:
            raise SystemExit(str(exc))
        self.broadcast = env.get("WOL_BROADCAST", "").strip() or "255.255.255.255"
        self.host = env.get("WOL_HOST", "").strip() or None
        self.name = env.get("WOL_NAME", "").strip() or "PC"
        self.listen = env.get("WOL_LISTEN", "").strip() or "0.0.0.0"
        self.port = int(env.get("WOL_PORT", "") or 8090)


class StatusCache:
    """Damit mehrere offene Handys den PC nicht im Sekundentakt anpingen."""

    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.value = None
        self.stamp = 0.0

    def get(self):
        if not self.host:
            return None
        with self.lock:
            if time.monotonic() - self.stamp > STATUS_CACHE_SECONDS:
                self.value = is_online(self.host)
                self.stamp = time.monotonic()
            return self.value

    def invalidate(self):
        with self.lock:
            self.stamp = 0.0


# ---------------------------------------------------------------------------
# Webseite
# ---------------------------------------------------------------------------

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#1f6feb"/>
<path d="M32 14v18" stroke="#fff" stroke-width="6" stroke-linecap="round"/>
<path d="M21.5 21.5a16 16 0 1 0 21 0" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round"/>
</svg>"""

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f6f7f9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111418" media="(prefers-color-scheme: dark)">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<title>__NAME__ aufwecken</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #ffffff; --text: #16191d; --muted: #646b75; --line: #e3e6ea;
    --accent: #1f6feb; --accent-text: #ffffff;
    --on: #1a7f37; --on-soft: #dcf3e2; --off: #8a919b; --off-soft: #eceef1;
    --wait: #b35900; --wait-soft: #fdebd8; --err: #c62828; --err-soft: #fbe3e3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #111418; --card: #1a1e24; --text: #e8ebef; --muted: #9aa3ad; --line: #2a3038;
      --accent: #4c8dff; --accent-text: #0b1220;
      --on: #4ac26b; --on-soft: #16301f; --off: #7d8590; --off-soft: #23282f;
      --wait: #f0a35c; --wait-soft: #3a2816; --err: #ff7b72; --err-soft: #3b1d1d;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--bg); color: var(--text);
    font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    padding: max(16px, env(safe-area-inset-top)) 16px max(16px, env(safe-area-inset-bottom));
  }
  main {
    width: 100%; max-width: 380px; background: var(--card);
    border: 1px solid var(--line); border-radius: 24px;
    padding: 28px 24px 22px; text-align: center;
  }
  h1 { font-size: 1.5rem; margin: 0 0 10px; letter-spacing: -0.01em; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px; border-radius: 999px; font-weight: 600; font-size: .95rem;
    background: var(--off-soft); color: var(--off);
  }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: currentColor; }
  [data-state="on"] .pill { background: var(--on-soft); color: var(--on); }
  [data-state="waking"] .pill { background: var(--wait-soft); color: var(--wait); }
  [data-state="waking"] .dot { animation: pulse 1.2s ease-in-out infinite; }
  [data-state="timeout"] .pill, [data-state="error"] .pill { background: var(--err-soft); color: var(--err); }
  @keyframes pulse { 50% { opacity: .25; } }

  .power {
    margin: 30px auto 18px; width: 168px; height: 168px; border-radius: 50%;
    border: 0; cursor: pointer; display: grid; place-items: center;
    background: var(--accent); color: var(--accent-text);
    box-shadow: 0 10px 30px -12px var(--accent);
    transition: transform .12s ease, opacity .2s ease, background .2s ease;
    -webkit-tap-highlight-color: transparent;
  }
  .power:active { transform: scale(.96); }
  .power:disabled { cursor: default; box-shadow: none; }
  [data-state="on"] .power { background: var(--on); }
  [data-state="waking"] .power:disabled { opacity: .55; }
  .power svg { width: 64px; height: 64px; }
  .power:focus-visible { outline: 3px solid var(--text); outline-offset: 4px; }

  .label { font-size: 1.1rem; font-weight: 600; margin: 0; }
  .hint { color: var(--muted); font-size: .92rem; margin: 6px 0 0; min-height: 2.6em; }
  footer { color: var(--muted); font-size: .8rem; margin-top: 18px; border-top: 1px solid var(--line); padding-top: 12px; }
</style>
</head>
<body>
<main id="app" data-state="unknown" data-has-host="__HAS_HOST__">
  <h1>__NAME__</h1>
  <span class="pill" role="status" aria-live="polite"><span class="dot"></span><span id="pill-text">Status wird geladen …</span></span>

  <button class="power" id="wake" type="button" aria-describedby="hint">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
      <path d="M12 3v9"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>
    </svg>
  </button>
  <p class="label" id="label">Aufwecken</p>
  <p class="hint" id="hint"></p>

  <footer>WoL-Relais auf __SERVER__</footer>
</main>
<script>
(() => {
  const app = document.getElementById('app');
  const btn = document.getElementById('wake');
  const pill = document.getElementById('pill-text');
  const label = document.getElementById('label');
  const hint = document.getElementById('hint');
  const hasHost = app.dataset.hasHost === '1';
  const WAKE_TIMEOUT_MS = 3 * 60 * 1000;
  let online = null, wakingSince = null, sentAt = null, failed = false, timer = null;

  const clock = d => d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });

  function render() {
    let state;
    if (failed) state = 'error';
    else if (online === true) { state = 'on'; wakingSince = null; }
    else if (wakingSince && Date.now() - wakingSince < WAKE_TIMEOUT_MS) state = 'waking';
    else if (wakingSince) state = 'timeout';
    else if (online === false) state = 'off';
    else state = 'unknown';
    app.dataset.state = state;

    const texts = {
      on:      ['Ist an', 'Läuft', 'Der PC ist eingeschaltet und erreichbar.'],
      off:     ['Ist aus', 'Aufwecken', 'Tippen schickt das Magic Packet über den Server ins Heimnetz.'],
      waking:  ['Fährt hoch …', 'Gesendet', `Paket um ${sentAt ? clock(sentAt) : ''} gesendet. Das dauert meist 20 bis 60 Sekunden.`],
      timeout: ['Keine Antwort', 'Nochmal senden', 'Der PC hat sich nach 3 Minuten nicht gemeldet. Siehe „Fehlersuche“ in der Anleitung.'],
      error:   ['Server nicht erreichbar', 'Nochmal versuchen', 'Ist Tailscale auf dem Handy an?'],
      unknown: [hasHost ? 'Status wird geladen …' : 'Status unbekannt', 'Aufwecken',
                hasHost ? '' : (sentAt ? `Paket um ${clock(sentAt)} gesendet.` : 'Für die Anzeige fehlt die IP des PCs (WOL_HOST).')],
    };
    const [p, l, h] = texts[state];
    pill.textContent = p;
    label.textContent = l;
    hint.textContent = h;
    btn.disabled = state === 'on' || state === 'waking';
    btn.setAttribute('aria-label', state === 'on' ? 'PC läuft bereits' : 'PC aufwecken');
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      const r = await fetch('api/status', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      online = (await r.json()).online;
      failed = false;
    } catch (e) {
      failed = true;
    }
    render();
    const fast = wakingSince && !failed && online !== true;
    timer = setTimeout(refresh, fast ? 2500 : 8000);
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await fetch('api/wake', { method: 'POST' });
      if (!r.ok) throw new Error(r.status);
      sentAt = new Date();
      wakingSince = hasHost ? Date.now() : null;
      failed = false;
      if (navigator.vibrate) navigator.vibrate(30);
    } catch (e) {
      failed = true;
    }
    render();
    refresh();
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  render();
  refresh();
})();
</script>
</body>
</html>
"""


def render_page(cfg):
    replacements = {
        "__NAME__": html.escape(cfg.name),
        "__HAS_HOST__": "1" if cfg.host else "0",
        "__SERVER__": html.escape(socket.gethostname()),
    }
    page = PAGE
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page.encode("utf-8")


def client_allowed(address):
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS if net.version == ip.version)


def make_handler(cfg, status):
    page = render_page(cfg)

    class Handler(BaseHTTPRequestHandler):
        def version_string(self):
            return "WoL-Relais"

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _path(self):
            return self.path.split("?", 1)[0].split("#", 1)[0]

        def _guard(self):
            if client_allowed(self.client_address[0]):
                return True
            log(f"abgewiesen: {self.client_address[0]} {self.command} {self._path()}")
            self._json(403, {"ok": False, "error": "nicht erlaubt"})
            return False

        def _status_payload(self):
            return {"name": cfg.name, "host": cfg.host, "online": status.get()}

        def do_GET(self):
            if not self._guard():
                return
            path = self._path()
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(200, self._status_payload())
            elif path == "/favicon.svg":
                self._send(200, FAVICON.encode("utf-8"), "image/svg+xml")
            elif path == "/api/wake":
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json(404, {"ok": False, "error": "nicht gefunden"})

        do_HEAD = do_GET

        def do_POST(self):
            if not self._guard():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 65536))  # Inhalt wird nicht gebraucht
            if self._path() != "/api/wake":
                self._json(404, {"ok": False, "error": "nicht gefunden"})
                return
            try:
                send_magic_packet(cfg.mac, cfg.broadcast)
            except OSError as exc:
                log(f"FEHLER beim Senden an {cfg.broadcast}: {exc}")
                self._json(500, {"ok": False, "error": f"Senden fehlgeschlagen: {exc}"})
                return
            status.invalidate()
            log(f"{self.client_address[0]} weckt {cfg.name} ({format_mac(cfg.mac)} via {cfg.broadcast})")
            self._json(200, {"ok": True, "name": cfg.name, "mac": format_mac(cfg.mac),
                             "broadcast": cfg.broadcast})

        def log_message(self, fmt, *args):
            pass  # Statusabfragen nicht mitschreiben, nur Wecken und Fehler (siehe log())

    return Handler


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Handy hat die Seite geschlossen, waehrend der Status noch geprueft wurde:
        # kein Fehler, also auch keinen Traceback ins Log schreiben.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def serve(cfg):
    status = StatusCache(cfg.host)
    if ":" in cfg.listen:
        Server.address_family = socket.AF_INET6
    httpd = Server((cfg.listen, cfg.port), make_handler(cfg, status))
    log(f"WoL-Relais laeuft auf {cfg.listen}:{cfg.port} fuer {cfg.name} "
        f"({format_mac(cfg.mac)}, Broadcast {cfg.broadcast}, Status-IP {cfg.host or '-'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv):
    command = argv[1] if len(argv) > 1 else "serve"
    if command in ("-h", "--help", "hilfe"):
        print(__doc__)
        return 0
    cfg = Config(os.environ)
    if command == "serve":
        serve(cfg)
        return 0
    if command == "wake":
        send_magic_packet(cfg.mac, cfg.broadcast)
        print(f"Magic Packet fuer {cfg.name} ({format_mac(cfg.mac)}) an {cfg.broadcast} gesendet.")
        return 0
    if command == "status":
        if not cfg.host:
            print("Status unbekannt: WOL_HOST ist nicht gesetzt.")
            return 2
        online = is_online(cfg.host)
        print(f"{cfg.name} ({cfg.host}) ist {'AN' if online else 'AUS bzw. nicht erreichbar'}.")
        return 0 if online else 1
    print(f"Unbekannter Befehl {command!r}. Erlaubt: serve, wake, status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
