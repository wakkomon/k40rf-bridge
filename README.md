# k40rf-bridge

Bosch Connect-Key **K 40 RF** → **MQTT** Bridge, z. B. für openHAB oder Home Assistant.

Liest die **lokale REST-API** des K 40 RF (ab Firmware 15.00.01) und veröffentlicht alle
verfügbaren Werte der Wärmepumpe per MQTT, optional mit Home-Assistant-Discovery
(die auch das openHAB-MQTT-Binding versteht). Komplett lokal, ohne Bosch-Cloud.

**Nur lesend:** Die lokale API bietet keine Schreib-Endpunkte, an der Wärmepumpe
wird nichts verändert.

Getestet mit Bosch Compress 6800i AW (Inneneinheit meldet sich als CS5800iAW 12 E,
Außeneinheit AW 12 OR-T). Laut Bosch sind u. a. CS 5800/6800 AW, CS 3800 AW und
CS 8800 AW mit K 40 RF kompatibel, ebenso Buderus WLW176/186i mit MX400.

## Funktionsweise

- Beim Start prüft die Bridge, welche Komponenten die Anlage hat (Wärmeerzeuger,
  Heizkreise, Warmwasser, Solar, Lüftung …) und welche der rund 260 dokumentierten
  Datenpunkte sie wirklich liefert. Dazu kommen die anlagenspezifischen `/signals`.
- Danach liest sie alle 30 s (einstellbar) und veröffentlicht geänderte Werte als
  *retained* MQTT-Nachrichten unter `k40rf/<API-Pfad>`.
- Einmal am Tag wird die Liste der Datenpunkte neu ermittelt.

## Deployment mit Portainer

1. *Stacks → Add stack → **Repository***
   - Repository URL: `https://github.com/wakkomon/k40rf-bridge`
   - Bei privatem Repo: *Authentication* aktivieren, GitHub-Benutzer + Personal Access Token
   - Compose path: `docker-compose.yml`
2. Unter *Environment variables* die Werte aus [`.env.example`](.env.example) setzen,
   mindestens `K40_HOST`, `MQTT_HOST` und fürs erste Pairing `K40_USERNAME` / `K40_PASSWORD`.
3. **Deploy the stack.** Portainer baut das Image selbst.

Updates: Stack öffnen → **Pull and redeploy**.

Ohne Portainer: `cp .env.example .env`, ausfüllen, `docker compose up -d --build`.

## Pairing (einmalig)

Login und Pass stehen auf dem Aufkleber des K 40 RF (auch in der Kurzanleitung, auf
der Verpackung oder im QR-Code: `V:1;L:<Login>;P:<Pass>;MAC:...;N:K40RF`).

1. Container starten und warten, bis im Log `PAIRING: Jetzt am K 40 RF ...` steht.
2. Am K 40 RF **WLAN- und Funk-Taste gleichzeitig ca. 1 s** drücken (LEDs blinken kurz blau).
3. Innerhalb von 5 Minuten meldet das Log `Pairing erfolgreich`. Die Bridge versucht es
   15 Minuten lang alle 10 s.

Das Token liegt danach im Volume `k40rf-data` und läuft nicht ab. `K40_USERNAME` und
`K40_PASSWORD` können anschließend wieder entfernt werden.

| Fehler | Bedeutung |
|---|---|
| `invalid_grant` | Login oder Pass falsch (Pass ohne Bindestriche, `0`/`O`, `1`/`l` prüfen) |
| `412 physical_proximity_unproven` | Tasten nicht (rechtzeitig) gedrückt **oder** Docker-Host nicht im selben Subnetz wie der K 40 RF |

Token manuell per curl holen (Pass ohne Bindestriche):

```bash
curl --insecure -X POST "https://<K40-IP>:9442/auth/token" \
  --data-urlencode "grant_type=password" --data-urlencode "username=<LOGIN>" \
  --data-urlencode "password=<PASS>" --data-urlencode "client_name=openhab-bridge"
```

Das `access_token` dann als `K40_TOKEN` setzen. Vorhandene Tokens auflisten:
`curl --insecure -H "Authorization: Bearer <TOKEN>" "https://<K40-IP>:9442/auth/token"`.

## MQTT-Topics

| Topic | Inhalt |
|---|---|
| `k40rf/status` | `online` / `offline` |
| `k40rf/paths` | JSON-Liste aller gefundenen Datenpunkte |
| `k40rf/<API-Pfad>` | Wert, z. B. `k40rf/heatSources/returnTemperature` → `36` |
| `k40rf/<API-Pfad>/label` | Status-Text, wenn kein Messwert anliegt (z. B. `OFF_HEAT`) |
| `k40rf/heatSources/emon/totalConsumption/<teil>` | Energiezähler in kWh (`outputProduced`, `compressor`, `eheater`) |
| `k40rf/signals/<Signal>` | Zusatzsignale, z. B. `k40rf/signals/SRC.OutdoorTemp` |

Ja/Nein-Werte kommen als `ON`/`OFF`, Aufzählungen als Text (`HEATING`, `IDLE` …).

### Wichtige Datenpunkte

| Topic | Bedeutung |
|---|---|
| `system/sensors/temperatures/outdoor_t1` | Außentemperatur |
| `heatSources/actualSupplyTemperature` | System-Vorlauf |
| `signals/SRC.IntFlowTemp` | interner Vorlauf der Inneneinheit |
| `heatSources/returnTemperature` | Rücklauf |
| `heatSources/currentSupplySetpoint` | Vorlauf-Sollwert |
| `dhwCircuits/dhw1/actualTemp` / `currentSetpoint` | Warmwasser Ist / Soll |
| `heatSources/compressor/powerElecActual` | el. Leistung Verdichter (W) |
| `heatSources/eHeater/powerElecActual` | el. Leistung Heizstab |
| `heatSources/actualHeatDemand` | `ch` Heizung, `dhw` Warmwasser, `frost`, leer = Stillstand |
| `heatSources/hs1/...` | Außeneinheit: Verdichterdrehzahl, Lüfter (`oduFanSpeed`), Kältekreis-Temperaturen |
| `heatSources/systemPressure` | Anlagendruck (bar) |
| `heatSources/emon/totalConsumption/*` | Wärme- und Stromzähler → JAZ = Wärme ÷ (Verdichter + Heizstab) |

Beschreibungen aller Pfade: [Bosch API-Beschreibung](https://github.com/bosch-home-comfort/api-docs).
Die `/signals` sind dort nicht dokumentiert, ihre Namen sind aber meist sprechend
(`SRC.CUHP.HP1.*` = Außeneinheit).

## openHAB

**Automatisch:** MQTT Binding installieren, Broker-Bridge anlegen. Mit `HA_DISCOVERY=true`
erscheint „Bosch Waermepumpe (K 40 RF)“ in der Inbox.

**Manuell** (`things/waermepumpe.things`):

```
Thing mqtt:topic:wp "Wärmepumpe" (mqtt:broker:mosquitto) {
  Channels:
    Type number : aussen     "Außentemperatur"  [ stateTopic="k40rf/system/sensors/temperatures/outdoor_t1", unit="°C" ]
    Type number : vorlauf    "Vorlauf"          [ stateTopic="k40rf/heatSources/actualSupplyTemperature", unit="°C" ]
    Type number : ruecklauf  "Rücklauf"         [ stateTopic="k40rf/heatSources/returnTemperature", unit="°C" ]
    Type number : ww         "Warmwasser"       [ stateTopic="k40rf/dhwCircuits/dhw1/actualTemp", unit="°C" ]
    Type number : leistung   "Verdichter el."   [ stateTopic="k40rf/heatSources/compressor/powerElecActual", unit="W" ]
    Type number : druck      "Anlagendruck"     [ stateTopic="k40rf/heatSources/systemPressure", unit="bar" ]
    Type number : waerme     "Wärme gesamt"     [ stateTopic="k40rf/heatSources/emon/totalConsumption/outputProduced", unit="kWh" ]
    Type number : strom      "Strom Verdichter" [ stateTopic="k40rf/heatSources/emon/totalConsumption/compressor", unit="kWh" ]
    Type string : modus      "Betriebsart"      [ stateTopic="k40rf/signals/SC.SeasonOpt.Mode" ]
}
```

```
Number:Temperature WP_Aussen    "Außen [%.1f %unit%]"      { channel="mqtt:topic:wp:aussen" }
Number:Temperature WP_Vorlauf   "Vorlauf [%.1f %unit%]"    { channel="mqtt:topic:wp:vorlauf" }
Number:Temperature WP_WW        "Warmwasser [%.1f %unit%]" { channel="mqtt:topic:wp:ww" }
Number:Power       WP_Leistung  "Leistung [%.0f %unit%]"   { channel="mqtt:topic:wp:leistung" }
Number:Energy      WP_Waerme    "Wärme [%.1f %unit%]"      { channel="mqtt:topic:wp:waerme" }
Number:Energy      WP_Strom     "Strom [%.1f %unit%]"      { channel="mqtt:topic:wp:strom" }
```

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `K40_HOST` | – | IP des K 40 RF (feste IP im Router empfohlen) |
| `K40_USERNAME` / `K40_PASSWORD` | – | Zugangsdaten vom Aufkleber, nur fürs Pairing |
| `K40_QR` | – | alternativ der komplette QR-Text |
| `K40_TOKEN` | – | alternativ ein vorhandenes Token |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` | Broker |
| `MQTT_USER` / `MQTT_PASSWORD` | – | Broker-Login |
| `MQTT_BASE_TOPIC` | `k40rf` | Basis-Topic |
| `POLL_INTERVAL` | `30` | Leseintervall in Sekunden (min. 10) |
| `REPUBLISH_MINUTES` | `15` | alle X Minuten alle Werte erneut senden |
| `REDISCOVER_HOURS` | `24` | Datenpunkte alle X Stunden neu ermitteln |
| `INCLUDE_SIGNALS` | `true` | `/signals` mit auslesen |
| `HA_DISCOVERY` | `true` | Discovery-Nachrichten senden |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Discovery-Präfix |
| `PAIRING_TIMEOUT_MIN` | `15` | Wartezeit aufs Tasten-Pairing |
| `LOG_LEVEL` | `INFO` | `DEBUG` für Fehlersuche |

## Fehlersuche

- **`Failed to resolve 'pypi.org'` beim Build:** DNS des Docker-Hosts prüfen
  (`docker run --rm busybox nslookup pypi.org`), ggf. in `/etc/docker/daemon.json`
  `{"dns": ["<Router-IP>"]}` setzen.
- **Token abgelehnt:** Volume `k40rf-data` (bzw. `/data/token.json`) löschen und neu pairen.
- **Keine Werte:** Firmware des K 40 RF prüfen (≥ 15.00.01), `LOG_LEVEL=DEBUG` setzen.

## Lizenz und Quellen

Apache-2.0, siehe [LICENSE](LICENSE).

- [pyk40rf](https://github.com/luc-ass/pyk40rf) (Apache-2.0, Beta) – Client für die lokale API
- [bosch-home-comfort/api-docs](https://github.com/bosch-home-comfort/api-docs) (Apache-2.0) –
  offizielle API-Beschreibung; `paths.json` ist daraus abgeleitet
- [bosch-buderus-wp.github.io – Local API](https://bosch-buderus-wp.github.io/en/docs/smarthome/local-api/)

Kein offizielles Bosch-Produkt und nicht mit Bosch verbunden.
