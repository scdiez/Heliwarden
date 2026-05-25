"""
modulo_calibracion.py — Calibración web-driven del sistema Heliwarden.

Responsabilidades:
  - Mover la cámara PTZ vía CGI (igual que modulo_patrulla).
  - Guiar al operador paso a paso publicando instrucciones en heliwarden/log.
  - Recibir comandos del frontend vía MQTT → heliwarden/calibracion/cmd.
  - Guardar config.json y ref_N.png en la carpeta 'calibracion/'.

Fases por preset:
  1. POSICION  — mover con A/D, confirmar con ENTER.
  2. ROI       — clics en la imagen del frontend, confirmar con ENTER.
  3. ANCLA     — selección de rectángulo en el frontend, confirmar con ENTER.

Corre integrado en app.py (importado), NO como proceso independiente.
"""

from __future__ import annotations

import cv2
import json
import numpy as np
import os
import requests
import threading
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────
# El módulo vive en Modulos/; los datos se guardan en la raíz del proyecto
_THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR  = _THIS_DIR.parent if _THIS_DIR.name == "Modulos" else _THIS_DIR
CALIB_DIR = ROOT_DIR / "calibracion"

load_dotenv(ROOT_DIR / ".env")

# ── Config cámara ─────────────────────────────────────────────────────────────
USER      = os.getenv("CAMERA_USER")
PASS      = os.getenv("CAMERA_PASS")
IP        = os.getenv("CAMERA_IP")
HTTP_PORT = int(os.getenv("CAMERA_HTTP_PORT", 88))

CGI_BASE  = f"http://{IP}:{HTTP_PORT}/cgi-bin/CGIProxy.fcgi"
SNAP_URL  = f"{CGI_BASE}?cmd=snapPicture2&usr={USER}&pwd={PASS}"

NUM_PRESETS = 3          # Número de posiciones a calibrar

# ── Estados de la máquina de calibración ─────────────────────────────────────
STATE_IDLE     = "idle"
STATE_POSICION = "posicion"   # Mover cámara
STATE_ROI      = "roi"        # Dibujar polígono ROI
STATE_ANCLA    = "ancla"      # Seleccionar rectángulo de referencia
STATE_DONE     = "done"


class Calibrador:
    """Máquina de estados que guía la calibración de los presets."""

    def __init__(self, mqtt_client: mqtt.Client):
        self.client    = mqtt_client
        self._lock     = threading.Lock()
        self._thread: threading.Thread | None = None

        # Estado interno
        self.state     = STATE_IDLE
        self.preset_idx = 0          # 0-based
        self.curr_pan  = 0           # pasos de pan acumulados
        self.misiones: list[dict] = []

        # Datos de la fase ROI/ANCLA en curso
        self.roi_puntos: list[list[int]] = []
        self.ancla_rect: list[int] = []   # [x, y, w, h]

        # Evento para sincronizar fases (el worker espera confirmación del frontend)
        self._cmd_event  = threading.Event()
        self._cmd_payload: dict = {}

        CALIB_DIR.mkdir(parents=True, exist_ok=True)

    # ── Publicar al log del frontend ──────────────────────────────────────────

    def _log(self, nivel: str, mensaje: str) -> None:
        payload = json.dumps({"nivel": nivel, "mensaje": mensaje})
        self.client.publish("heliwarden/log", payload, qos=1)
        print(f"[calibracion] [{nivel}] {mensaje}")

    def _instruccion(self, texto: str) -> None:
        """Publica como nivel especial CALIB para que el frontend lo resalte."""
        payload = json.dumps({"nivel": "CALIB", "mensaje": texto})
        self.client.publish("heliwarden/log", payload, qos=1)
        print(f"[calibracion] [INST] {texto}")

    # ── Notifica al frontend el estado actual ─────────────────────────────────

    def _pub_estado(self, extra: dict | None = None) -> None:
        payload = {
            "state":      self.state,
            "preset":     self.preset_idx + 1,
            "total":      NUM_PRESETS,
            "roi_puntos": self.roi_puntos,
        }
        if extra:
            payload.update(extra)
        self.client.publish(
            "heliwarden/calibracion/estado",
            json.dumps(payload),
            qos=1,
        )

    # ── PTZ helpers (mismos comandos CGI que modulo_patrulla) ────────────────

    def _cgi(self, cmd: str, extra: dict | None = None) -> None:
        params = {"cmd": cmd, "usr": USER, "pwd": PASS}
        if extra:
            params.update(extra)
        try:
            requests.get(CGI_BASE, params=params, timeout=4)
        except Exception as e:
            print(f"[calibracion] CGI error: {e}")

    def _mover(self, direction: str, duration: float, speed: int = 3) -> None:
        dir_cmd = {
            "left":  "ptzMoveLeft",
            "right": "ptzMoveRight",
        }
        cmd = dir_cmd.get(direction)
        if not cmd:
            return
        self._cgi(cmd, {"speed": speed})
        time.sleep(duration)
        self._cgi("ptzStopRun")
        time.sleep(0.4)

    def _home(self) -> None:
        self._instruccion("Sincronizando posición HOME — barriendo hacia la derecha...")
        self._cgi("ptzMoveRight", {"speed": 9})
        time.sleep(15.0)
        self._cgi("ptzStopRun")
        time.sleep(1.0)
        self.curr_pan = 0
        self._instruccion("HOME alcanzado. ¡Listo para calibrar!")

    def _snap(self) -> np.ndarray | None:
        try:
            r = requests.get(SNAP_URL, timeout=5)
            arr = np.frombuffer(r.content, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return cv2.rotate(img, cv2.ROTATE_180) if img is not None else None
        except Exception:
            return None

    # ── Guardar preset en la cámara ───────────────────────────────────────────

    def _guardar_preset_camara(self, preset_id: int) -> None:
        """Guarda la posición actual como preset en la cámara Foscam."""
        try:
            r = requests.get(
                CGI_BASE,
                params={"cmd": "ptzAddPresetPoint", "name": preset_id,
                        "usr": USER, "pwd": PASS},
                timeout=4,
            )
            ok = r.status_code == 200 and "<result>0</result>" in r.text
            if ok:
                self._log("INFO", f"Preset {preset_id} guardado en la cámara.")
            else:
                self._log("ALARM", f"No se pudo guardar preset {preset_id} en la cámara (resp: {r.text[:80]})")
        except Exception as e:
            self._log("ALARM", f"Error guardando preset {preset_id}: {e}")

    # ── Procesar comando del frontend ─────────────────────────────────────────

    def recibir_comando(self, payload: dict) -> None:
        """Llamado desde el callback MQTT del hilo principal."""
        accion = payload.get("accion")

        if accion == "iniciar" and self.state == STATE_IDLE:
            self._arrancar()
            return

        if accion == "cancelar":
            self._cancelar()
            return

        if self.state == STATE_POSICION:
            if accion == "mover_izq":
                self._mover("left", 0.5)
                self.curr_pan -= 1
                self._instruccion(
                    f"◀ Movido a la izquierda. Pan actual: {self.curr_pan}. "
                    f"Pulsa A (izq) / D (der) o CONFIRMAR cuando estés en posición."
                )
                self._pub_estado()
            elif accion == "mover_der":
                self._mover("right", 0.5)
                self.curr_pan += 1
                self._instruccion(
                    f"▶ Movido a la derecha. Pan actual: {self.curr_pan}. "
                    f"Pulsa A (izq) / D (der) o CONFIRMAR cuando estés en posición."
                )
                self._pub_estado()
            elif accion == "confirmar":
                self._cmd_payload = payload
                self._cmd_event.set()

        elif self.state == STATE_ROI:
            if accion == "roi_punto":
                x, y = int(payload.get("x", 0)), int(payload.get("y", 0))
                self.roi_puntos.append([x, y])
                self._instruccion(
                    f"📍 Punto {len(self.roi_puntos)} del ROI añadido ({x}, {y}). "
                    f"Sigue haciendo clic o CONFIRMAR para cerrar el polígono."
                )
                self._pub_estado()
            elif accion == "roi_deshacer":
                if self.roi_puntos:
                    self.roi_puntos.pop()
                    self._instruccion(f"↩ Último punto eliminado. Quedan {len(self.roi_puntos)} puntos.")
                    self._pub_estado()
            elif accion == "confirmar":
                if len(self.roi_puntos) < 3:
                    self._instruccion("⚠️  Necesitas al menos 3 puntos para definir el ROI.")
                else:
                    self._cmd_payload = payload
                    self._cmd_event.set()

        elif self.state == STATE_ANCLA:
            if accion == "ancla_rect":
                self.ancla_rect = [
                    int(payload.get("x", 0)), int(payload.get("y", 0)),
                    int(payload.get("w", 0)), int(payload.get("h", 0)),
                ]
                self._instruccion(
                    f"🔲 Rectángulo de anclaje definido: "
                    f"({self.ancla_rect[0]}, {self.ancla_rect[1]}) "
                    f"{self.ancla_rect[2]}×{self.ancla_rect[3]}px. "
                    f"CONFIRMAR para guardar."
                )
                self._pub_estado({"ancla_rect": self.ancla_rect})
            elif accion == "confirmar":
                if not self.ancla_rect:
                    self._instruccion("⚠️  Primero selecciona el rectángulo de anclaje (parasol).")
                else:
                    self._cmd_payload = payload
                    self._cmd_event.set()

    # ── Arranque y cancelación ────────────────────────────────────────────────

    def _arrancar(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _cancelar(self) -> None:
        self.state = STATE_IDLE
        self._cmd_event.set()   # desbloquea el worker si estuviera esperando
        self._log("INFO", "Calibración cancelada.")
        self._pub_estado()

    # ── Worker principal ──────────────────────────────────────────────────────

    def _esperar_confirmacion(self) -> bool:
        """Bloquea hasta que llegue 'confirmar' o se cancele. Devuelve False si cancelado."""
        self._cmd_event.clear()
        self._cmd_event.wait()
        if self.state == STATE_IDLE:   # cancelado externamente
            return False
        return True

    def _worker(self) -> None:
        self.misiones.clear()
        self.preset_idx = 0

        self._log("INFO", "═══ INICIO DE CALIBRACIÓN ═══")
        self._instruccion(
            "Calibración iniciada. Voy a sincronizar la posición HOME de la cámara."
        )

        self._home()

        for i in range(NUM_PRESETS):
            if self.state == STATE_IDLE:
                return   # cancelado

            self.preset_idx = i
            preset_id = i + 1   # IDs 1-based

            # ── FASE 1: POSICIÓN ─────────────────────────────────────────────
            self.state = STATE_POSICION
            self._pub_estado()
            self._instruccion(
                f"━━ POSICIÓN {preset_id}/{NUM_PRESETS} ━━  "
                f"Usa los botones ◀ A  D ▶ del panel de calibración para apuntar la cámara. "
                f"Pulsa CONFIRMAR cuando estés en posición."
            )

            if not self._esperar_confirmacion():
                return

            pan_guardado = self.curr_pan
            self._guardar_preset_camara(preset_id)
            self._log("INFO", f"✅ Posición {preset_id} confirmada (pan={pan_guardado}).")

            # ── FASE 2: ROI ──────────────────────────────────────────────────
            self.state = STATE_ROI
            self.roi_puntos = []
            self._pub_estado()
            self._instruccion(
                f"━━ ROI {preset_id}/{NUM_PRESETS} ━━  "
                f"Haz clic sobre el vídeo para marcar el área de vigilancia (mínimo 3 puntos). "
                f"Pulsa DESHACER para borrar el último punto. "
                f"Pulsa CONFIRMAR cuando el polígono esté cerrado."
            )

            if not self._esperar_confirmacion():
                return

            roi_guardado = [list(p) for p in self.roi_puntos]
            self._log("INFO", f"✅ ROI {preset_id} confirmado ({len(roi_guardado)} puntos).")

            # ── FASE 3: ANCLAJE ──────────────────────────────────────────────
            self.state = STATE_ANCLA
            self.ancla_rect = []
            self._pub_estado()
            self._instruccion(
                f"━━ ANCLAJE {preset_id}/{NUM_PRESETS} ━━  "
                f"Haz clic y arrastra sobre el vídeo para seleccionar la referencia fija "
                f"(p. ej. el parasol o un elemento estático). "
                f"Pulsa CONFIRMAR para guardar."
            )

            if not self._esperar_confirmacion():
                return

            x, y, w, h = self.ancla_rect
            centro = [x + w // 2, y + h // 2]

            # Capturar imagen y recortar plantilla
            img = self._snap()
            plantilla = None
            if img is not None:
                plantilla = img[y:y + h, x:x + w]
                ref_path = CALIB_DIR / f"ref_{preset_id}.png"
                cv2.imwrite(str(ref_path), plantilla)
                self._log("INFO", f"✅ Referencia guardada: {ref_path.name}")
            else:
                self._log("ALARM", f"⚠️  No se pudo capturar imagen para ref_{preset_id}.png")

            self.misiones.append({
                "id":      preset_id,
                "pan_abs": pan_guardado,
                "roi":     roi_guardado,
                "centro":  centro,
            })

            self._log(
                "INFO",
                f"✅ Preset {preset_id} completado — pan={pan_guardado}, "
                f"centro=({centro[0]}, {centro[1]}), ROI={len(roi_guardado)} puntos.",
            )

        # ── Guardar config.json ───────────────────────────────────────────────
        config_path = CALIB_DIR / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.misiones, f, indent=4)

        self.state = STATE_DONE
        self._pub_estado()
        self._log("INFO", "═══ CALIBRACIÓN COMPLETADA ═══  config.json guardado.")
        self._instruccion(
            "🎉 Calibración finalizada correctamente. "
            "Puedes cerrar este panel e iniciar la patrulla."
        )

        # Volver a idle tras un momento
        time.sleep(2)
        self.state = STATE_IDLE
        self._pub_estado()