"""
modulos/modulo_conexion.py — Vigilante de conexión.

Comprueba periódicamente que la cámara responde (HTTP snapshot).
Publica el estado en MQTT → heliwarden/conexion.
No envía correos (eso lo hace fusion_estados.py).

Corre como proceso independiente: `python modulos/modulo_conexion.py`
"""

import json
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

# ── Paths basados en la ubicación de este archivo ─────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

# ── Configuración ─────────────────────────────────────────────────────────────

USER      = os.getenv("CAMERA_USER")
PASS      = os.getenv("CAMERA_PASS")
IP        = os.getenv("CAMERA_IP")
HTTP_PORT = int(os.getenv("CAMERA_HTTP_PORT", 88))

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT   = int(os.getenv("MQTT_PORT"))

INTERVALO_CHECK = int(os.getenv("INTERVALO_CHECK_CONEXION", 5))

HTTP_SNAP_URL = (
    f"http://{IP}:{HTTP_PORT}/cgi-bin/CGIProxy.fcgi"
    f"?cmd=snapPicture2&usr={USER}&pwd={PASS}"
)

TOPIC_CONEXION = "heliwarden/conexion"

# ── Estado ────────────────────────────────────────────────────────────────────

_online        = True
_t_desconexion = None


# ── MQTT ──────────────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ modulo_conexion conectado al broker MQTT.")
    else:
        print(f"❌ modulo_conexion: fallo MQTT (rc={rc})")


def _on_disconnect(client, userdata, rc):
    print(f"⚠️  modulo_conexion desconectado del broker (rc={rc}). Reintentando...")


def _publicar(client: mqtt.Client, online: bool, segundos_caida: int = 0) -> None:
    payload = json.dumps({"online": online, "segundos_caida": segundos_caida})
    client.publish(TOPIC_CONEXION, payload, qos=1, retain=True)


# ── Comprobación de cámara ────────────────────────────────────────────────────

def _camara_responde() -> bool:
    try:
        r = requests.get(HTTP_SNAP_URL, timeout=3)
        return r.status_code == 200 and len(r.content) > 1000
    except Exception:
        return False


# ── Bucle principal ───────────────────────────────────────────────────────────

def _bucle(client: mqtt.Client) -> None:
    global _online, _t_desconexion

    while True:
        responde = _camara_responde()

        if responde and not _online:
            _online        = True
            _t_desconexion = None
            print("✅ Cámara recuperada.")
            _publicar(client, online=True)

        elif not responde and _online:
            _online        = False
            _t_desconexion = time.time()
            print("❌ Cámara no responde.")
            _publicar(client, online=False, segundos_caida=0)

        elif not responde and not _online:
            elapsed = int(time.time() - _t_desconexion)
            print(f"❌ Cámara sin señal: {elapsed}s")
            _publicar(client, online=False, segundos_caida=elapsed)

        time.sleep(INTERVALO_CHECK)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = mqtt.Client(client_id="heliwarden-conexion")
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    print(f"Conectando a broker MQTT en {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    try:
        _bucle(client)
    except KeyboardInterrupt:
        print("\nmodulo_conexion detenido.")
        client.loop_stop()
        client.disconnect()
