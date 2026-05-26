"""
Modulos/modulo_patrulla.py — Control PTZ y seguimiento de referencias.

Responsabilidades:
  - Mover la cámara PTZ a través de los presets configurados.
  - Detectar referencias visuales con template matching.
  - Publicar resultado de cada preset en MQTT → heliwarden/patrulla
  - Escuchar comandos en MQTT → heliwarden/patrulla/cmd

Control PTZ vía CGI HTTP nativo Foscam (SD4H).
NO usa ONVIF.
NO escribe en helipuertos.json directamente.
NO envía correos.
NO llama a YOLO (eso es modulo_deteccion.py).

Corre como proceso independiente: python modulos/modulo_patrulla.py
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

CALIB_DIR = ROOT_DIR / "calibracion"

# Velocidad de movimiento continuo (1–9 en Foscam)
PTZ_SPEED = int(os.getenv("PTZ_SPEED", 4))

# Tiempo para barrer el eje X completo a velocidad máxima
PTZ_HOME_LEFT = float(os.getenv("PTZ_HOME_LEFT", 15.0))

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


def _snap_img() -> np.ndarray | None:
    """Obtiene un frame vía HTTP snapshot y lo devuelve como ndarray BGR."""
    try:
        r = _cgi("snapPicture2", timeout=3)
        if r.status_code != 200 or len(r.content) < 1000:
            return None
        arr = np.frombuffer(r.content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return cv2.rotate(img, cv2.ROTATE_180) if img is not None else None
    except Exception:
        return None


# Mapeo de dirección → comando CGI Foscam
_DIR_CMD = {
    "left":       "ptzMoveLeft",
    "right":      "ptzMoveRight",
    "up":         "ptzMoveUp",
    "down":       "ptzMoveDown",
    "stop":       "ptzStopRun",
    "topleft":    "ptzMoveTopLeft",
    "topright":   "ptzMoveTopRight",
    "bottomleft": "ptzMoveBottomLeft",
    "bottomright":"ptzMoveBottomRight",
}


def _ptz_move(direction: str, duration: float, speed: int = PTZ_SPEED) -> None:
    """Mueve la cámara en `direction` durante `duration` segundos y la para."""
    cmd = _DIR_CMD.get(direction)
    if cmd is None:
        print(f"[PTZ] Dirección desconocida: {direction}")
        return
    try:
        _cgi(cmd, {"speed": speed})
        time.sleep(duration)
        _cgi("ptzStopRun")
        time.sleep(0.3)
    except Exception as e:
        print(f"[PTZ] Error en movimiento '{direction}': {e}")


def _ptz_preset(preset_id: int) -> bool:
    """Mueve la cámara al preset guardado. Devuelve True si la respuesta es OK."""
    try:
        r = _cgi("ptzGotoPresetPoint", {"name": preset_id})
        # Foscam devuelve XML con <result>0</result> si va bien
        return r.status_code == 200 and "<result>0</result>" in r.text
    except Exception as e:
        print(f"[PTZ] Error yendo a preset {preset_id}: {e}")
        return False


def _ptz_home() -> None:
    """Mueve la cámara al extremo izquierdo para sincronizar posición X."""
    print("[PTZ] Sincronizando HOME (barrido izquierda)...")
    _ptz_move("right", PTZ_HOME_LEFT, speed=9)
    print("[PTZ] HOME alcanzado.")


# ── Patrullero ────────────────────────────────────────────────────────────────

class Patrullero:
    def __init__(self, mqtt_client: mqtt.Client):
        self.client = mqtt_client

        config_path = CALIB_DIR / "config.json"
        if not config_path.exists():
            print("⚠️  No existe calibracion/config.json")
            self.config = []
        else:
            with open(config_path, "r") as f:
                self.config = json.load(f)

        self.last_p            = 0
        self.patrulla_thread   = None
        self.stop_event        = threading.Event()
        self.indice_actual     = 0
        self.fallos_precision  = {}
        self.intentos_fallidos = {}
        self._ack_events: dict = {}

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def _pub_estado(self, id_preset: int, ref_ok: bool, fallos: int, razon: str = "") -> None:
        payload = json.dumps({
            "preset":        id_preset,
            "referencia_ok": ref_ok,
            "fallos":        fallos,
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

    # ── Imagen ────────────────────────────────────────────────────────────────

    def get_img(self) -> np.ndarray | None:
        return _snap_img()

    # ── Ajuste fino (template matching) ──────────────────────────────────────

    def ajustar_af(self, mision: dict):
        ref_path  = str(CALIB_DIR / f"ref_{mision['id']}.png")
        plantilla = cv2.imread(ref_path)
        if plantilla is None:
            return False, "PRECISION"

        fallos_brutos = 0
        for i in range(40):
            if self.stop_event.is_set():
                return False, "STOP"

            img = self.get_img()
            if img is None:
                recuperado = self._esperar_conexion()
                if not recuperado:
                    return False, "STOP"
                continue

            res = cv2.matchTemplate(img, plantilla, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val < 0.92:
                fallos_brutos += 1
                if fallos_brutos >= 5:
                    return False, "PRECISION"
                time.sleep(0.5)
                continue
            else:
                fallos_brutos = 0

            curr_c = (max_loc[0] + plantilla.shape[1] // 2, max_loc[1] + plantilla.shape[0] // 2)
            dx     = mision["centro"][0] - curr_c[0]
            dist_x = abs(dx)

            print(f"      [Paso {i+1}] Error X: {dx} | Conf: {max_val:.2f}")

            if dist_x <= 20:
                return True, "OK"

            # Velocidad y duración proporcionales al error
            if dist_x > 300:   speed, t_x = 4, 0.20
            elif dist_x > 100: speed, t_x = 3, 0.12
            elif dist_x > 40:  speed, t_x = 2, 0.07
            else:               speed, t_x = 1, 0.04

            _ptz_move("right" if dx > 0 else "left", t_x, speed=speed)
            time.sleep(0.8)

        return False, "OSCILACION"

    # ── Misión por preset ─────────────────────────────────────────────────────

    def ejecutar_mision(self, mision: dict) -> bool:
        if self.stop_event.is_set():
            return False

        id_preset = mision["id"]
        plantilla = cv2.imread(str(CALIB_DIR / f"ref_{id_preset}.png"))
        if plantilla is None:
            print(f"[Patrulla] Sin imagen de referencia para preset {id_preset}")
            return False

        # Ir al preset guardado en cámara
        print(f"[Patrulla] → Preset {id_preset}")
        ok = _ptz_preset(id_preset)
        if not ok:
            print(f"[Patrulla] ptzGotoPresetPoint falló para preset {id_preset}, intentando búsqueda manual...")

        # Dar tiempo a que la cámara llegue
        time.sleep(2.0)

        # Verificar referencia visual
        encontrada = False
        pasos      = 0
        max_pasos  = 12

        while pasos < max_pasos:
            if self.stop_event.is_set():
                return False

            img = self.get_img()
            if img is None:
                recuperado = self._esperar_conexion()
                if not recuperado:
                    return False
                pasos = 0
                continue

            res = cv2.matchTemplate(img, plantilla, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            print(f"   Búsqueda P{id_preset} paso {pasos+1}/{max_pasos} | Match: {max_val:.2f}")

            if max_val >= 0.92:
                encontrada = True
                break

        direccion = "left" if id_preset > self.last_p else "right"
        # El primer paso de toda la patrulla (last_p=0) usa un impulso mínimo
        t_mov = 0.03 if self.last_p == 0 else 0.08
        _ptz_move(direccion, t_mov, speed=1)
        pasos += 1
        time.sleep(0.3)

        if not encontrada:
            self.fallos_precision[id_preset] = self.fallos_precision.get(id_preset, 0) + 1
            self._pub_estado(id_preset, ref_ok=False,
                             fallos=self.fallos_precision[id_preset], razon="NO_REFERENCIA")
            return False

        exito, razon = self.ajustar_af(mision)

        if exito:
            self.fallos_precision[id_preset]  = 0
            self.intentos_fallidos[id_preset] = 0
            self.last_p = id_preset
            self._pub_estado(id_preset, ref_ok=True, fallos=0)

            ruta = guardar_captura_hd(id_preset)
            if ruta:
                ev = threading.Event()
                self._ack_events[id_preset] = ev
                self._pub_captura(id_preset, ruta)

                print(f"   ⏳ Esperando análisis YOLO del preset {id_preset}...")
                recibido = ev.wait(timeout=DETECCION_TIMEOUT)
                del self._ack_events[id_preset]

                if recibido:
                    print(f"   ✅ ACK recibido para preset {id_preset}.")
                else:
                    print(f"   ⚠️  Timeout ACK preset {id_preset} ({DETECCION_TIMEOUT}s). Continuando.")
            return True
        else:
            if self.stop_event.is_set():
                return False

            if razon == "PRECISION":
                self.fallos_precision[id_preset] = self.fallos_precision.get(id_preset, 0) + 1
                self._pub_estado(id_preset, ref_ok=False,
                                 fallos=self.fallos_precision[id_preset], razon="PRECISION")
            elif razon == "OSCILACION":
                self.intentos_fallidos[id_preset] = self.intentos_fallidos.get(id_preset, 0) + 1
                self._pub_estado(id_preset, ref_ok=False,
                                 fallos=self.intentos_fallidos[id_preset], razon="OSCILACION")
            return False

    # ── Espera reconexión ─────────────────────────────────────────────────────

    def _esperar_conexion(self) -> bool:
        print("[Patrulla] CONEXIÓN PERDIDA — esperando...")
        while not self.stop_event.is_set():
            img = _snap_img()
            if img is not None:
                print("[Patrulla] Conexión recuperada.")
                _ptz_home()
                return True
            time.sleep(5)
        return False

    # ── Bucle de patrulla ─────────────────────────────────────────────────────

    def _bucle_worker(self) -> None:
        print("[Patrulla] Iniciando patrulla...")
        self.client.publish(
            "heliwarden/patrulla/reset",
            json.dumps({"accion": "reset"}),
            qos=1,
        )
        _ptz_home()

        while not self.stop_event.is_set():
            for idx in range(self.indice_actual, len(self.config)):
                self.indice_actual = idx
                if self.stop_event.is_set():
                    break
                self.ejecutar_mision(self.config[idx])

            self.indice_actual = 0
            print("[Patrulla] Ciclo completo. Reiniciando...")

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
            print("⚠️  Patrullero no inicializado aún.")
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


def _on_disconnect(client, userdata, rc):
    print(f"⚠️  modulo_patrulla desconectado del broker (rc={rc}). Reintentando...")


if __name__ == "__main__":
    client = mqtt.Client(client_id="heliwarden-patrulla")
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.on_disconnect = _on_disconnect
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