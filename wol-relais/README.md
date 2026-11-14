# WoL-Relais: PC vom Handy aus aufwecken, auch von unterwegs

```
Handy ──Tailscale──▶ Heimserver ──Magic Packet (Broadcast)──▶ PC
```

**Warum vom Handy direkt nicht geht:** Ein Magic Packet ist ein Broadcast. Der
geht nur im eigenen Heimnetz, nicht durchs Internet und nicht durch Tailscale.
Und auch zu Hause im WLAN filtern viele Router und Access Points Broadcasts
zwischen WLAN und LAN heraus.

**Die Lösung:** Der Heimserver läuft sowieso durchgehend und hängt im selben Netz
wie der PC. Das Handy sagt ihm über Tailscale Bescheid, und er schickt das Paket.
Auf dem Handy brauchst du nur eine Webseite mit einem großen Knopf, die anzeigt,
ob der PC an ist.

## Was der PC können muss (einmalig)

Am Server liegt es nicht, wenn der PC nicht aufwacht. Damit er reagiert, muss
Folgendes stimmen:

1. **Netzwerkkabel.** Über WLAN klappt Wake-on-LAN bei Desktop-PCs praktisch nie.
2. **BIOS/UEFI:** „Wake on LAN“, „Power On By PCI-E“ oder „Resume by LAN“ **an**,
   und „ErP“ bzw. „Deep Sleep“ **aus**. ErP schaltet die Netzwerkkarte im
   ausgeschalteten Zustand stromlos.
3. **Windows-Schnellstart aus:** Systemsteuerung → Energieoptionen → „Auswählen,
   was beim Drücken von Netzschaltern geschehen soll“ → „Einige Einstellungen sind
   momentan nicht verfügbar“ → Haken bei **„Schnellstart aktivieren“** entfernen.
4. **Netzwerkkarte** (Geräte-Manager → Netzwerkadapter → Ethernet-Adapter →
   Eigenschaften):
   - Reiter *Energieverwaltung*: „Gerät kann den Computer aus dem Ruhezustand
     aktivieren“ und „Nur Magic Packet …“ anhaken.
   - Reiter *Erweitert*: „Wake on Magic Packet“ **aktiviert**. Bei Realtek-Karten
     zusätzlich „Shutdown Wake-On-Lan“ **aktiviert**.

**Für die Installation brauchst du die IP und die MAC-Adresse des PCs.** In der
Eingabeaufforderung auf dem PC zeigt `ipconfig /all` beides beim
Ethernet-Adapter: „IPv4-Adresse“ und „Physische Adresse“.

Damit die Adresse gleich bleibt, solltest du dem PC im Router eine feste IP geben.
Bei der Fritzbox geht das so: Heimnetz → Netzwerk → PC bearbeiten → „Diesem
Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“.

## Installation auf dem Heimserver

Der PC sollte dabei **eingeschaltet** sein, dann findet das Skript seine
MAC-Adresse selbst.

```bash
git clone -b claude/funny-lovelace-tpkpkf https://github.com/KricEraser/WoLGit.git ~/WoLGit
```

```bash
sudo bash ~/WoLGit/wol-relais/install.sh 192.168.178.23
```

Ersetze `192.168.178.23` durch die IP deines PCs. Das Skript macht dann Folgendes:

- Es erkennt Netzwerkkarte, Broadcast-Adresse und die MAC-Adresse des PCs.
- Es installiert den Dienst `wol-relais`, der bei jedem Neustart von selbst startet.
- Es testet den Dienst und zeigt die Adressen für das Handy an.

Falls der PC gerade aus ist, gib die MAC-Adresse einfach mit an:
`sudo bash ~/WoLGit/wol-relais/install.sh 192.168.178.23 AA-BB-CC-DD-EE-FF`

Möglichkeiten zum Anpassen:

| Wie | Wirkung |
|---|---|
| `sudo WOL_NAME="Gaming-PC" bash …/install.sh …` | anderer Name auf der Webseite |
| `sudo WOL_PORT=8091 bash …/install.sh …` | anderer Port (Standard ist 8090) |
| `PROBELAUF=1 bash …/install.sh …` | nur prüfen und anzeigen, nichts installieren |

## Auf dem Handy

1. Die Tailscale-App muss an sein. Das ist dieselbe Verbindung, über die du auch
   sonst auf den Server kommst.
2. Öffne in Chrome die Adresse, die das Skript am Ende als „unterwegs“ anzeigt,
   zum Beispiel `http://100.x.y.z:8090`.
3. Tippe auf ⋮ und dann auf **„Zum Startbildschirm hinzufügen“**.

Ab jetzt reicht ein Tipp auf das Symbol, zu Hause wie unterwegs. Die Seite zeigt
„Ist aus“, „Fährt hoch …“ oder „Ist an“.

## Auf dem Server selbst

```bash
pc-wecken
```

```bash
pc-wecken status
```

## Fehlersuche

**Die Seite lädt nicht:** Prüf, ob Tailscale auf dem Handy an ist, und schau dann
auf dem Server nach dem Dienst:

```bash
systemctl status wol-relais
```

**„Keine Antwort“ nach dem Wecken:** Das Paket ist raus, aber der PC reagiert
nicht. Teste das zuerst auf dem Server mit `pc-wecken`, während der PC **im
Energiesparmodus** ist (nicht ganz aus):

- Er wacht dann auf, aber nicht aus dem ausgeschalteten Zustand: Dann blockiert
  noch der Schnellstart oder ErP im BIOS (Punkte 2 und 3 oben).
- Er wacht auch dann nicht auf: Prüf die Einstellungen der Netzwerkkarte (Punkt 4)
  und ob nach dem Herunterfahren an der Netzwerkbuchse noch eine LED leuchtet.
  Leuchtet keine, ist die Karte stromlos, und das liegt am BIOS.

**Der PC ist an, aber die Seite zeigt „Ist aus“:** Die Anzeige prüft per Ping und
über die Ports 445, 3389, 135 und 22. Wenn Windows alles davon blockiert, erlaubst
du Ping mit diesem Befehl in einer Administrator-PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Ping erlauben (WoL-Relais)" -Protocol ICMPv4 -IcmpType 8 -Action Allow
```

**Log ansehen:** Jedes Wecken wird mit Uhrzeit und Absender protokolliert.

```bash
journalctl -u wol-relais -n 30
```

## Sicherheit

- Der Dienst kann nur eines: das Magic Packet an **deinen** PC schicken. Er nimmt
  keine Befehle oder Adressen von außen an.
- Er antwortet nur Geräten aus dem Heimnetz und aus deinem Tailnet. Aus dem
  Internet ist er nicht erreichbar, solange du im Router keinen Port freigibst.
  **Gib auch keinen frei.**
- Er läuft als eingeschränkter Systembenutzer ohne Schreibrechte. Die einzige
  Sonderberechtigung ist Ping für die An/Aus-Anzeige.

## Entfernen

```bash
sudo systemctl disable --now wol-relais
```

```bash
sudo rm -r /opt/wol-relais /etc/wol-relais.env /etc/systemd/system/wol-relais.service /usr/local/bin/pc-wecken
```

## Für Home Assistant und Co.

Andere Programme können den Dienst auch direkt ansprechen:

- `POST http://<server>:8090/api/wake` weckt den PC.
- `GET http://<server>:8090/api/status` liefert zum Beispiel
  `{"name": "PC", "host": "192.168.178.23", "online": true}`.

Damit lässt sich der PC zum Beispiel als Kachel in die Gerätesteuerung von Home
Assistant einbinden.
