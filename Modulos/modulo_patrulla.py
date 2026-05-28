"""
Modulos/modulo_patrulla.py — Control PTZ por Presets Directos.

Responsabilidades:
  - Mover la cámara PTZ secuencialmente a través de los presets 1, 2 y 3.
  - Publicar el estado de haber alcanzado el preset en MQTT → heliwarden/patrulla
  - Tomar captura HD y enviarla al flujo para análisis YOLO.
  - Escuchar comandos en MQTT → heliwarden/patrulla/cmd

Control PTZ vía CGI HTTP nativo Foscam (SD4H).
"""

import cv2
import json
import numpy as np
import os
import requests
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent
sys.path.insert(0, str(MODULOS_DIR))

from captura_hd import guardar_captura_hd

load_dotenv(ROOT_DIR / ".env")

# ── Configuración ─────────────────────────────────────────────────────────────

USER      = os.getenv("CAMERA_USER")
PASS      = os.getenv("CAMERA_PASS")
IP        = os.getenv("CAMERA_IP")
HTTP_PORT = int(os.getenv("CAMERA_HTTP_PORT", 88))

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

TOPIC_ESTADO  = "heliwarden/patrulla"
TOPIC_CMD     = "heliwarden/patrulla/cmd"
TOPIC_CAPTURA = "heliwarden/patrulla/captura"
TOPIC_ACK     = "heliwarden/deteccion/ack"

DETECCION_TIMEOUT = 90

# ── CGI PTZ helper ────────────────────────────────────────────────────────────

def _cgi(cmd: str, extra: dict = None, timeout: float = 4.0) -> requests.Response:
    """Llama a la API CGI de la Foscam."""
    params = {"cmd": cmd, "usr": USER, "pwd": PASS}
    if extra:
        params.update(extra)
    url = f"http://{IP}:{HTTP_PORT}/cgi-bin/CGIProxy.fcgi"
    return requests.get(url, params=params, timeout=timeout)


def _ptz_preset(preset_id: int) -> bool:
    """Mueve la cámara al preset guardado. Devuelve True si la respuesta es OK."""
    try:
        r = _cgi("ptzGotoPresetPoint", {"name": preset_id})
        return r.status_code == 200 and "<result>0</result>" in r.text
    except Exception as e:
        print(f"[PTZ] Error yendo a preset {preset_id}: {e}")
        return False


# ── Patrullero ────────────────────────────────────────────────────────────────

class Patrullero:
    def __init__(self, mqtt_client: mqtt.Client):
        self.client = mqtt_client
        # Seteamos directamente la lista fija con tus 3 presets
        self.presets_mision = [1, 2, 3]
        
        self.patrulla_thread   = None
        self.stop_event        = threading.Event()
        self.indice_actual     = 0
        self._ack_events: dict = {}

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def _pub_estado(self, id_preset: int, ref_ok: bool, razon: str = "") -> None:
        payload = json.dumps({
            "preset":        id_preset,
            "referencia_ok": ref_ok,  # Se envía True si llegó correctamente al preset
            "fallos":        0,
            "razon":         razon,
        })
        self.client.publish(TOPIC_ESTADO, payload, qos=1)

    def _pub_captura(self, id_preset: int, ruta: str) -> None:
        payload = json.dumps({"preset": id_preset, "ruta": ruta})
        self.client.publish(TOPIC_CAPTURA, payload, qos=1)

    def notificar_ack(self, id_preset: int) -> None:
        ev = self._ack_events.get(id_preset)
        if ev is not None:
            ev.set()

    # ── Misión por preset simplificada ────────────────────────────────────────

    def ejecutar_mision(self, id_preset: int) -> bool:
        if self.stop_event.is_set():
            return False

        print(f"[Patrulla] → Moviendo a Preset {id_preset}...")
        ok = _ptz_preset(id_preset)
        
        if not ok:
            print(f"[Patrulla] ❌ ptzGotoPresetPoint falló para el preset {id_preset}")
            self._pub_estado(id_preset, ref_ok=False, razon="FALLO_PRESET")
            return False

        # Tiempo de espera generoso para que la cámara termine de girar y estabilice la imagen
        time.sleep(3.5)

        if self.stop_event.is_set():
            return False

        # Notificamos que se ha alcanzado la posición del preset con éxito
        self._pub_estado(id_preset, ref_ok=True, razon="OK")

        # Guardar captura HD y mandar a YOLO tal y como hacía tu código original
        ruta = guardar_captura_hd(id_preset)
        if ruta:
            ev = threading.Event()
            self._ack_events[id_preset] = ev
            self._pub_captura(id_preset, ruta)

            print(f"   ⏳ Esperando análisis YOLO del preset {id_preset}...")
            recibido = ev.wait(timeout=DETECCION_TIMEOUT)
            
            try:
                del self._ack_events[id_preset]
            except KeyError:
                pass

            if recibido:
                print(f"   ✅ ACK recibido para preset {id_preset}.")
            else:
                print(f"   ⚠️ Timeout ACK preset {id_preset} ({DETECCION_TIMEOUT}s). Continuando.")
        
        return True

    # ── Bucle de patrulla ─────────────────────────────────────────────────────

    # ── Bucle de patrulla modificado ──────────────────────────────────────────

    def _bucle_worker(self) -> None:
        print("[Patrulla] Iniciando patrulla por presets directos...")
        self.client.publish(
            "heliwarden/patrulla/reset",
            json.dumps({"accion": "reset"}),
            qos=1,
        )

        while not self.stop_event.is_set():
            
            # --- RUTINA DE CALIBRACIÓN ANTES DE EMPEZAR EL CICLO ---
            if self.indice_actual == 0:
                print("[PTZ] Calibrando posición antes del Preset 1...")
                
                # OPCIÓN A: Usar el Preset 2 (que va muy bien) para estabilizar el motor
                _ptz_preset(2) 
                time.sleep(3.0)
                
                # OPCIÓN B: Si prefieres el barrido al extremo izquierdo (descomenta la línea de abajo)
                # _ptz_move("right", duration=9.0, speed=9) # Ajustado a 9.0s para evitar el fallo del eje Y
                # time.sleep(1.0)
            # -------------------------------------------------------

            for idx in range(self.indice_actual, len(self.presets_mision)):
                self.indice_actual = idx
                if self.stop_event.is_set():
                    break
                
                preset_actual = self.presets_mision[idx]
                self.ejecutar_mision(preset_actual)
                
                # Pausa entre presets
                time.sleep(1.0)

            self.indice_actual = 0
            if not self.stop_event.is_set():
                print("[Patrulla] Ciclo completo. Reiniciando en 5 segundos...")
                time.sleep(5.0)

    def iniciar_patrulla(self) -> None:
        if self.patrulla_thread is None or not self.patrulla_thread.is_alive():
            self.stop_event.clear()
            self.patrulla_thread = threading.Thread(target=self._bucle_worker, daemon=True)
            self.patrulla_thread.start()
            print("[Patrulla] Patrulla iniciada.")

    def stop_patrulla(self) -> None:
        self.stop_event.set()
        _cgi("ptzStopRun")
        if self.patrulla_thread:
            self.patrulla_thread.join(timeout=2.0)
        print("[Patrulla] Patrulla detenida.")


# ── Main ──────────────────────────────────────────────────────────────────────

_patrullero: Patrullero = None

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ modulo_patrulla conectado al broker MQTT.")
        client.subscribe([(TOPIC_CMD, 1), (TOPIC_ACK, 1)])
    else:
        print(f"❌ modulo_patrulla: fallo MQTT (rc={rc})")

def _on_message(client, userdata, msg):
    global _patrullero
    try:
        payload = json.loads(msg.payload.decode())
        topic   = msg.topic

        if _patrullero is None:
            return

        if topic == TOPIC_CMD:
            accion = payload.get("accion")
            if accion == "iniciar":
                _patrullero.iniciar_patrulla()
            elif accion == "detener":
                _patrullero.stop_patrulla()

        elif topic == TOPIC_ACK:
            id_preset = payload.get("preset")
            if id_preset is not None:
                _patrullero.notificar_ack(id_preset)

    except Exception as e:
        print(f"[modulo_patrulla] Error en comando: {e}")

if __name__ == "__main__":
    client = mqtt.Client(client_id="heliwarden-patrulla")
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    print(f"Conectando a broker MQTT en {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    try:
        _patrullero = Patrullero(mqtt_client=client)
        print("✅ Patrullero inicializado. Esperando comandos...")
    except Exception as e:
        print(f"❌ No se pudo inicializar el Patrullero: {e}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nmodulo_patrulla detenido.")
        if _patrullero:
            _patrullero.stop_patrulla()
        client.loop_stop()
        client.disconnect()