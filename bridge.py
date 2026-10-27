"""Bosch Connect-Key K 40 RF -> MQTT bridge.

Reads the local REST API of the K 40 RF (firmware >= 15.00.01) via pyk40rf
and publishes every available value to MQTT, optionally with Home-Assistant
discovery messages (openHAB's MQTT binding understands those, too).

Read-only: nothing is ever written to the heat pump.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

import aiohttp
import aiomqtt
from pyk40rf import (
    EnergyResource,
    K40AuthError,
    K40Client,
    K40ConnectionError,
    K40Error,
    K40ProximityError,
    NumericResource,
    RawResource,
    Resource,
    StringResource,
    parse_sticker_qr,
)

# --------------------------------------------------------------------------
# configuration (environment)
# --------------------------------------------------------------------------


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


K40_HOST = env("K40_HOST")
K40_USERNAME = env("K40_USERNAME")
K40_PASSWORD = env("K40_PASSWORD")
K40_QR = env("K40_QR")
K40_TOKEN = env("K40_TOKEN")

MQTT_HOST = env("MQTT_HOST", "localhost")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USER = env("MQTT_USER")
MQTT_PASSWORD = env("MQTT_PASSWORD")
BASE = (env("MQTT_BASE_TOPIC", "k40rf") or "k40rf").rstrip("/")

POLL_INTERVAL = max(10, int(env("POLL_INTERVAL", "30")))
REPUBLISH_MINUTES = int(env("REPUBLISH_MINUTES", "15"))
REDISCOVER_HOURS = float(env("REDISCOVER_HOURS", "24"))
INCLUDE_SIGNALS = env_bool("INCLUDE_SIGNALS", True)
HA_DISCOVERY = env_bool("HA_DISCOVERY", True)
HA_PREFIX = (env("HA_DISCOVERY_PREFIX", "homeassistant") or "homeassistant").rstrip("/")
PAIRING_TIMEOUT_MIN = int(env("PAIRING_TIMEOUT_MIN", "15"))

DATA_DIR = Path(env("DATA_DIR", "/data") or "/data")
TOKEN_FILE = DATA_DIR / "token.json"
PATHS_FILE = Path(env("PATHS_FILE", str(Path(__file__).with_name("paths.json"))))

logging.basicConfig(
    level=(env("LOG_LEVEL", "INFO") or "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("k40rf-bridge")
logging.getLogger("pyk40rf").setLevel(logging.WARNING)

ID_SEGMENT = re.compile(r"^(hs|hc|dhw|sc|zone|device)\d+$")


# --------------------------------------------------------------------------
# token handling / pairing
# --------------------------------------------------------------------------


def credentials() -> tuple[str | None, str | None]:
    if K40_QR:
        creds = parse_sticker_qr(K40_QR)
        return creds.username, creds.password
    return K40_USERNAME, K40_PASSWORD


def load_token() -> str | None:
    if K40_TOKEN:
        return K40_TOKEN
    try:
        return json.loads(TOKEN_FILE.read_text())["access_token"]
    except (OSError, ValueError, KeyError):
        return None


def save_token(token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"access_token": token, "created": int(time.time())}))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


async def pair(client: K40Client) -> str:
    username, password = credentials()
    if not username or not password:
        raise SystemExit(
            "Kein Token vorhanden und keine Zugangsdaten gesetzt. "
            "Bitte K40_USERNAME + K40_PASSWORD (vom Aufkleber des K 40 RF) "
            "oder K40_QR setzen."
        )

    deadline = time.monotonic() + PAIRING_TIMEOUT_MIN * 60
    log.warning("=" * 70)
    log.warning("PAIRING: Jetzt am K 40 RF kurz die WLAN-Taste UND die Funk-Taste druecken")
    log.warning("(je ca. 1 Sekunde). Ich versuche es %d Minuten lang alle 10 s.", PAIRING_TIMEOUT_MIN)
    log.warning("=" * 70)
    while True:
        try:
            token = await client.async_request_token(username, password, client_name="k40rf-mqtt-bridge")
        except K40ProximityError:
            if time.monotonic() > deadline:
                raise SystemExit(
                    "Pairing-Zeit abgelaufen. Tasten druecken und Container neu starten. "
                    "Hinweis: Der Docker-Host muss im selben Subnetz wie der K 40 RF sein."
                ) from None
            log.info("Warte auf Tastendruck am K 40 RF ...")
            await asyncio.sleep(10)
            continue
        except K40AuthError as err:
            raise SystemExit(f"Zugangsdaten abgelehnt: {err}") from err
        except K40ConnectionError as err:
            log.warning("K 40 RF nicht erreichbar (%s), neuer Versuch in 10 s", err)
            await asyncio.sleep(10)
            continue
        save_token(token.access_token)
        log.info("Pairing erfolgreich, Token gespeichert in %s", TOKEN_FILE)
        return token.access_token


# --------------------------------------------------------------------------
# discovery of available paths
# --------------------------------------------------------------------------


async def discover_paths(client: K40Client) -> list[str]:
    candidates: list[str] = json.loads(PATHS_FILE.read_text())
    installation = await client.async_discover_installation()
    allowed = {
        "heatSources": set(installation.heat_sources),
        "heatingCircuits": set(installation.heating_circuits),
        "dhwCircuits": set(installation.dhw_circuits),
        "solarCircuits": set(installation.solar_circuits),
        "ventilation": set(installation.ventilation_zones),
        "zones": set(installation.zones),
        "devices": set(installation.devices),
    }
    log.info(
        "Anlage: Waermeerzeuger=%s Heizkreise=%s WW=%s Solar=%s Lueftung=%s Zonen=%s Geraete=%s",
        *(sorted(v) or "-" for v in allowed.values()),
    )

    def wanted(path: str) -> bool:
        parts = path.strip("/").split("/")
        root = parts[0]
        for seg in parts[1:]:
            if ID_SEGMENT.match(seg):
                return seg in allowed.get(root, set())
        return True

    paths = [p for p in candidates if wanted(p)]
    log.info("Pruefe %d moegliche Datenpunkte ...", len(paths))
    found = await client.async_get_many(paths)
    available = sorted(found)

    if INCLUDE_SIGNALS:
        signals = list(installation.signals)
        if signals:
            found_signals = await client.async_get_many(signals)
            available += sorted(found_signals)
            log.info("%d Signale gefunden", len(found_signals))

    log.info("%d Datenpunkte verfuegbar", len(available))
    return available


# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------


def to_payloads(path: str, res: Resource) -> dict[str, str]:
    """Map a resource to {topic: payload}. Missing measurements publish no value."""
    topic = f"{BASE}{path}"
    out: dict[str, str] = {}

    if isinstance(res, NumericResource):
        if res.is_enum:
            out[topic] = res.label if res.label is not None else str(res.raw_value)
        elif res.value is not None:
            out[topic] = _num(res.value)
        label = res.label or res.error_label
        out[f"{topic}/label"] = label or ""
    elif isinstance(res, StringResource):
        if res.is_boolean:
            out[topic] = "ON" if res.boolean else "OFF"
        else:
            out[topic] = res.value or ""
    elif isinstance(res, EnergyResource):
        out[topic] = json.dumps(res.components)
        for key, val in res.components.items():
            out[f"{topic}/{key}"] = _num(val)
    elif isinstance(res, RawResource):
        values = res.raw.get("values")
        if isinstance(values, list) and all(isinstance(v, (str, int, float)) for v in values):
            out[topic] = ",".join(str(v) for v in values)  # arrayData, e.g. actualHeatDemand
        else:
            out[topic] = json.dumps(res.raw.get("values", res.raw), ensure_ascii=False)
    else:
        raw = getattr(res, "raw", {})
        out[topic] = json.dumps(raw.get("value", raw), ensure_ascii=False, default=str)
    return out


def _num(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


UNIT_MAP = {
    "C": ("°C", "temperature", "measurement"),
    "K": ("K", None, "measurement"),
    "kW": ("kW", "power", "measurement"),
    "W": ("W", "power", "measurement"),
    "kWh": ("kWh", "energy", "total_increasing"),
    "Wh": ("Wh", "energy", "total_increasing"),
    "bar": ("bar", "pressure", "measurement"),
    "%": ("%", None, "measurement"),
    "l/min": ("L/min", "volume_flow_rate", "measurement"),
    "rpm": ("rpm", None, "measurement"),
    "Hz": ("Hz", "frequency", "measurement"),
    "min": ("min", "duration", None),
    "mins": ("min", "duration", None),
    "s": ("s", "duration", None),
    "h": ("h", "duration", None),
}


def slug(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", path.strip("/")).strip("_").lower()


def pretty_name(path: str) -> str:
    parts = path.strip("/").split("/")
    if parts[0] == "signals" and len(parts) > 1:
        return parts[1]
    return " ".join(parts[1:] if len(parts) > 1 else parts)


def ha_configs(path: str, res: Resource, device: dict) -> dict[str, str]:
    """Home-Assistant discovery config(s) for one data point."""
    obj = slug(path)
    uid = f"{device['identifiers'][0]}_{obj}"
    state_topic = f"{BASE}{path}"
    base_cfg = {
        "name": pretty_name(path),
        "unique_id": uid,
        "object_id": uid,
        "state_topic": state_topic,
        "availability_topic": f"{BASE}/status",
        "device": device,
    }

    if isinstance(res, StringResource) and res.is_boolean:
        cfg = {**base_cfg, "payload_on": "ON", "payload_off": "OFF"}
        return {f"{HA_PREFIX}/binary_sensor/{uid}/config": json.dumps(cfg)}

    if isinstance(res, NumericResource) and not res.is_enum:
        cfg = dict(base_cfg)
        unit = UNIT_MAP.get(res.unit or "")
        if unit:
            cfg["unit_of_measurement"] = unit[0]
            if unit[1]:
                cfg["device_class"] = unit[1]
            if unit[2]:
                cfg["state_class"] = unit[2]
        elif res.unit:
            cfg["unit_of_measurement"] = res.unit
        return {f"{HA_PREFIX}/sensor/{uid}/config": json.dumps(cfg)}

    if isinstance(res, EnergyResource):
        configs = {}
        for key in res.components:
            cuid = f"{uid}_{slug(key)}"
            cfg = {
                **base_cfg,
                "name": f"{pretty_name(path)} {key}",
                "unique_id": cuid,
                "object_id": cuid,
                "state_topic": f"{state_topic}/{key}",
                "state_class": "total_increasing",
            }
            if res.unit:
                cfg["unit_of_measurement"] = res.unit
                if res.unit in ("kWh", "Wh"):
                    cfg["device_class"] = "energy"
            configs[f"{HA_PREFIX}/sensor/{cuid}/config"] = json.dumps(cfg)
        return configs

    if isinstance(res, (NumericResource, StringResource, RawResource)):
        return {f"{HA_PREFIX}/sensor/{uid}/config": json.dumps(base_cfg)}

    return {}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


class Bridge:
    def __init__(self) -> None:
        self.cache: dict[str, str] = {}
        self.last_full = 0.0
        self.discovery_sent: set[str] = set()

    async def publish(self, mqtt: aiomqtt.Client, payloads: dict[str, str], force: bool) -> int:
        count = 0
        for topic, payload in payloads.items():
            if not force and self.cache.get(topic) == payload:
                continue
            await mqtt.publish(topic, payload, qos=0, retain=True)
            self.cache[topic] = payload
            count += 1
        return count

    async def run(self) -> None:
        if not K40_HOST:
            raise SystemExit("K40_HOST ist nicht gesetzt (IP-Adresse des K 40 RF).")

        async with aiohttp.ClientSession() as session:
            client = K40Client(K40_HOST, session, verify_ssl=False)
            token = load_token() or await pair(client)
            client.token = token

            try:
                info = await client.async_get_system_info()
            except K40AuthError:
                log.error("Token wird abgelehnt. %s loeschen und neu pairen.", TOKEN_FILE)
                raise
            device_id = info.gateway_id or credentials()[0] or K40_HOST
            device = {
                "identifiers": [f"k40rf_{device_id}"],
                "name": "Bosch Waermepumpe (K 40 RF)",
                "manufacturer": "Bosch",
                "model": info.product_name or "K 40 RF",
            }
            if info.serial_number:
                device["serial_number"] = info.serial_number
            log.info("Verbunden mit Gateway %s (%s)", device_id, device["model"])

            will = aiomqtt.Will(f"{BASE}/status", "offline", qos=1, retain=True)
            while True:
                try:
                    async with aiomqtt.Client(
                        MQTT_HOST,
                        MQTT_PORT,
                        username=MQTT_USER,
                        password=MQTT_PASSWORD,
                        identifier=f"k40rf-bridge-{device_id}",
                        will=will,
                    ) as mqtt:
                        log.info("MQTT verbunden: %s:%s, Basis-Topic '%s'", MQTT_HOST, MQTT_PORT, BASE)
                        await mqtt.publish(f"{BASE}/status", "online", qos=1, retain=True)
                        self.cache.clear()
                        self.discovery_sent.clear()
                        try:
                            await self.loop(client, mqtt, device)
                        except asyncio.CancelledError:
                            await mqtt.publish(f"{BASE}/status", "offline", qos=1, retain=True)
                            raise
                except aiomqtt.MqttError as err:
                    log.warning("MQTT-Verbindung verloren (%s), neuer Versuch in 10 s", err)
                    await asyncio.sleep(10)

    async def loop(self, client: K40Client, mqtt: aiomqtt.Client, device: dict) -> None:
        paths: list[str] = []
        next_discovery = 0.0
        while True:
            started = time.monotonic()
            try:
                if not paths or started >= next_discovery:
                    paths = await discover_paths(client)
                    next_discovery = started + REDISCOVER_HOURS * 3600
                    await mqtt.publish(f"{BASE}/paths", json.dumps(paths), retain=True)

                resources = await client.async_get_many(paths)
                force = REPUBLISH_MINUTES > 0 and started - self.last_full >= REPUBLISH_MINUTES * 60
                payloads: dict[str, str] = {}
                for path, res in resources.items():
                    payloads.update(to_payloads(path, res))
                    if HA_DISCOVERY and path not in self.discovery_sent:
                        for topic, cfg in ha_configs(path, res, device).items():
                            await mqtt.publish(topic, cfg, retain=True)
                        self.discovery_sent.add(path)
                changed = await self.publish(mqtt, payloads, force)
                if force:
                    self.last_full = started
                await mqtt.publish(f"{BASE}/status", "online", qos=1, retain=True)
                log.debug("%d Werte gelesen, %d veroeffentlicht", len(resources), changed)
            except K40AuthError:
                log.error("Token abgelehnt. %s loeschen und Container neu starten (Pairing).", TOKEN_FILE)
                raise
            except K40Error as err:
                log.warning("Lesefehler am K 40 RF: %s", err)
                await mqtt.publish(f"{BASE}/status", "offline", qos=1, retain=True)

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, POLL_INTERVAL - elapsed))


def main() -> None:
    loop = asyncio.new_event_loop()
    task = loop.create_task(Bridge().run())
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        log.info("Beendet.")


if __name__ == "__main__":
    main()
