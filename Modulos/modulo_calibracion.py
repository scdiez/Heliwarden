"""
modulo_calibracion.py — Calibración con ventana OpenCV nativa a pantalla completa.

Responsabilidades:
  - Al recibir el comando 'iniciar', lanza una ventana OpenCV fullscreen en el
    servidor (igual que el script config.py de referencia).
  - Interacción directa: A/D para mover, clic para ROI, arrastre para anclaje,
    ENTER para confirmar, ESC para cancelar.
  - Coordenadas siempre en píxeles de la imagen real → sin errores de escala.
  - Publica estado e instrucciones por MQTT igual que antes.
  - Guarda config.json y ref_N.png en calibracion/.
  - Mientras la ventana está abierta, publica heliwarden/calibracion/bloqueado
    para que el frontend pause sus refreshes automáticos.

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
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────
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

NUM_PRESETS = 3

# ── Colores BGR ───────────────────────────────────────────────────────────────
COL_ROI    = (80, 220, 80)      # verde
COL_ANCLA  = (40, 180, 255)     # naranja
COL_TEXT   = (255, 255, 255)
COL_SHADOW = (0, 0, 0)
COL_INST   = (0, 200, 255)      # amarillo-cian para instrucciones

# ── Estados ───────────────────────────────────────────────────────────────────
STATE_IDLE     = "idle"
STATE_HOME     = "home"
STATE_POSICION = "posicion"
STATE_ROI      = "roi"
STATE_ANCLA    = "ancla"
STATE_DONE     = "done"

WIN_NAME = "Heliwarden — Calibración (ESC para cancelar)"


# ── Helpers de dibujado ───────────────────────────────────────────────────────

def _text_shadow(img, texto, pos, escala=0.65, grosor=1, color=COL_TEXT):
    """Texto con sombra para mejor legibilidad sobre cualquier fondo."""
    x, y = pos
    cv2.putText(img, texto, (x + 1, y + 1), cv2.FONT_HERSHEY_SIMPLEX,
                escala, COL_SHADOW, grosor + 1, cv2.LINE_AA)
    cv2.putText(img, texto, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                escala, color, grosor, cv2.LINE_AA)


def _barra_instruccion(img, texto, h_img):
    """Barra semitransparente en la parte inferior con la instrucción actual."""
    overlay = img.copy()
    cv2.rectangle(overlay, (0, h_img - 50), (img.shape[1], h_img), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.75, img, 0.25, 0, img)
    _text_shadow(img, texto, (12, h_img - 18), escala=0.60, color=COL_INST)


def _barra_superior(img, texto_fase, preset_idx, num_presets):
    """Barra superior con fase y progreso."""
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 0), (img.shape[1], 38), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.75, img, 0.25, 0, img)
    _text_shadow(img, texto_fase, (12, 26), escala=0.70, color=COL_TEXT)
    prog = f"Preset {preset_idx + 1} / {num_presets}"
    tw, _ = cv2.getTextSize(prog, cv2.FONT_HERSHEY_SIMPLEX, 0.60, 1)[0], None
    _text_shadow(img, prog, (img.shape[1] - 160, 26), escala=0.60, color=(150, 200, 255))


# ── Clase principal ───────────────────────────────────────────────────────────

class Calibrador:
    """
    Cuando se llama a recibir_comando({'accion':'iniciar'}), lanza un hilo
    que abre una ventana OpenCV a pantalla completa y guía al operador.
    """

    def __init__(self, mqtt_client: mqtt.Client):
        self.client  = mqtt_client
        self._thread: threading.Thread | None = None
        self._lock   = threading.Lock()

        # Estado observable por app.py
        self.state      = STATE_IDLE
        self.preset_idx = 0
        self.roi_puntos: list[list[int]] = []
        self.ancla_rect: list[int] = []
        self.misiones:   list[dict] = []

        # Variables internas de la ventana OpenCV (solo accedidas desde el hilo)
        self._img_display: np.ndarray | None = None
        self._roi_pts_tmp: list[list[int]]   = []
        self._drag_start:  tuple | None      = None
        self._drag_end:    tuple | None      = None
        self._ancla_tmp:   list[int]         = []  # [x,y,w,h] en píxeles reales

        # Semáforos entre callback de ratón y bucle principal del hilo
        self._roi_nuevo_punto  = threading.Event()
        self._ancla_definida   = threading.Event()

        CALIB_DIR.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    # MQTT helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, nivel: str, msg: str) -> None:
        self.client.publish("heliwarden/log",
                            json.dumps({"nivel": nivel, "mensaje": msg}), qos=1)
        print(f"[calib][{nivel}] {msg}")

    def _instruccion(self, texto: str) -> None:
        self.client.publish("heliwarden/log",
                            json.dumps({"nivel": "CALIB", "mensaje": texto}), qos=1)
        print(f"[calib][INST] {texto}")

    def _pub_estado(self, extra: dict | None = None) -> None:
        payload = {
            "state":      self.state,
            "preset":     self.preset_idx + 1,
            "total":      NUM_PRESETS,
            "roi_puntos": self.roi_puntos,
            "ancla_rect": self.ancla_rect,
        }
        if extra:
            payload.update(extra)
        self.client.publish("heliwarden/calibracion/estado",
                            json.dumps(payload), qos=1)

    def _pub_bloqueado(self, bloqueado: bool) -> None:
        """Avisa al frontend de que debe pausar/reanudar sus refreshes."""
        self.client.publish("heliwarden/calibracion/bloqueado",
                            json.dumps({"bloqueado": bloqueado}), qos=1, retain=True)

    # ─────────────────────────────────────────────────────────────────────────
    # PTZ / Cámara
    # ─────────────────────────────────────────────────────────────────────────

    def _cgi(self, cmd: str, extra: dict | None = None) -> None:
        params = {"cmd": cmd, "usr": USER, "pwd": PASS}
        if extra:
            params.update(extra)
        try:
            requests.get(CGI_BASE, params=params, timeout=4)
        except Exception as e:
            print(f"[calib] CGI error: {e}")

    def _mover(self, direction: str, duration: float, speed: int = 3) -> None:
        cmd = {"left": "ptzMoveLeft", "right": "ptzMoveRight"}.get(direction)
        if not cmd:
            return
        self._cgi(cmd, {"speed": speed})
        time.sleep(duration)
        self._cgi("ptzStopRun")
        time.sleep(0.4)

    def _home(self) -> None:
        self._instruccion("Sincronizando HOME — barriendo hacia la derecha (15 s)...")
        self._cgi("ptzMoveRight", {"speed": 9})
        time.sleep(15.0)
        self._cgi("ptzStopRun")
        time.sleep(1.0)
        self._curr_pan = 0
        self._instruccion("HOME alcanzado.")

    def _snap(self) -> np.ndarray | None:
        try:
            r   = requests.get(SNAP_URL, timeout=5)
            arr = np.frombuffer(r.content, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return cv2.rotate(img, cv2.ROTATE_180) if img is not None else None
        except Exception:
            return None

    def _guardar_preset_camara(self, preset_id: int) -> None:
        try:
            r  = requests.get(CGI_BASE, params={"cmd": "ptzAddPresetPoint",
                                                "name": preset_id,
                                                "usr": USER, "pwd": PASS}, timeout=4)
            ok = r.status_code == 200 and "<result>0</result>" in r.text
            self._log("INFO" if ok else "ALARM",
                      f"Preset {preset_id} {'guardado' if ok else 'ERROR al guardar'} en cámara.")
        except Exception as e:
            self._log("ALARM", f"Error guardando preset {preset_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Ventana OpenCV — callbacks de ratón
    # ─────────────────────────────────────────────────────────────────────────

    def _mouse_roi(self, event, x, y, flags, param):
        """Callback para la fase ROI: clic izquierdo añade punto."""
        if event == cv2.EVENT_LBUTTONDOWN:
            self._roi_pts_tmp.append([x, y])
            self._roi_nuevo_punto.set()

    def _mouse_ancla(self, event, x, y, flags, param):
        """Callback para la fase ANCLAJE: arrastre define el rectángulo."""
        if event == cv2.EVENT_LBUTTONDOWN:
            self._drag_start = (x, y)
            self._drag_end   = (x, y)
            self._ancla_definida.clear()
        elif event == cv2.EVENT_MOUSEMOVE and self._drag_start:
            self._drag_end = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self._drag_start:
            self._drag_end = (x, y)
            x0 = min(self._drag_start[0], self._drag_end[0])
            y0 = min(self._drag_start[1], self._drag_end[1])
            w  = abs(self._drag_end[0] - self._drag_start[0])
            h  = abs(self._drag_end[1] - self._drag_start[1])
            if w > 5 and h > 5:
                self._ancla_tmp = [x0, y0, w, h]
                self._ancla_definida.set()
            self._drag_start = None

    # ─────────────────────────────────────────────────────────────────────────
    # Bucle de la ventana: refresca imagen y overlay continuamente
    # ─────────────────────────────────────────────────────────────────────────

    def _get_frame_display(self) -> np.ndarray | None:
        """Obtiene un frame fresco y lo escala a pantalla completa."""
        img = self._snap()
        if img is None:
            return None
        # Escalar al tamaño de la ventana manteniendo aspecto
        h_win, w_win = cv2.getWindowImageRect(WIN_NAME)[3], cv2.getWindowImageRect(WIN_NAME)[2]
        if w_win <= 0 or h_win <= 0:
            return img
        h_img, w_img = img.shape[:2]
        scale = min(w_win / w_img, h_win / h_img)
        new_w, new_h = int(w_img * scale), int(h_img * scale)
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        # Canvas negro del tamaño de la ventana
        canvas = np.zeros((h_win, w_win, 3), dtype=np.uint8)
        off_x  = (w_win - new_w) // 2
        off_y  = (h_win - new_h) // 2
        canvas[off_y:off_y + new_h, off_x:off_x + new_w] = resized
        return canvas

    # ─────────────────────────────────────────────────────────────────────────
    # Fases de calibración (se ejecutan dentro del hilo worker)
    # ─────────────────────────────────────────────────────────────────────────

    def _fase_posicion(self, preset_id: int) -> bool:
        """
        Muestra el stream en vivo.
        A/D mueven la cámara. ENTER confirma. ESC cancela.
        Devuelve True si se confirmó, False si se canceló.
        """
        self.state = STATE_POSICION
        self._pub_estado()
        instruccion = (f"[POSICIÓN {preset_id}/{NUM_PRESETS}]  "
                       f"A=izquierda  D=derecha  ENTER=confirmar  ESC=cancelar")
        self._instruccion(instruccion)

        cv2.setMouseCallback(WIN_NAME, lambda *a: None)  # sin callback de ratón

        while True:
            img = self._snap()
            if img is None:
                time.sleep(0.3)
                continue

            # Dibujar overlay
            frame = img.copy()
            _barra_superior(frame, f"POSICIÓN {preset_id}", self.preset_idx, NUM_PRESETS)
            _barra_instruccion(frame, instruccion, frame.shape[0])
            cv2.imshow(WIN_NAME, frame)

            key = cv2.waitKey(50) & 0xFF
            if key == 27:           # ESC → cancelar
                return False
            elif key in (ord('a'), ord('A')):
                self._mover("left", 0.5)
                self._curr_pan -= 1
            elif key in (ord('d'), ord('D')):
                self._mover("right", 0.5)
                self._curr_pan += 1
            elif key == 13:         # ENTER → confirmar
                return True

    def _fase_roi(self, preset_id: int, img_ref: np.ndarray) -> bool:
        """
        Permite al operador hacer clic para definir el polígono ROI.
        Z=deshacer, ENTER=confirmar, ESC=cancelar.
        Coordenadas en píxeles REALES de img_ref (sin escala CSS).
        Devuelve True si se confirmó.
        """
        self.state = STATE_ROI
        self._roi_pts_tmp = []
        self._pub_estado()
        instruccion = (f"[ROI {preset_id}/{NUM_PRESETS}]  "
                       f"CLIC=añadir punto  Z=deshacer  ENTER=confirmar (mín 3 pts)  ESC=cancelar")
        self._instruccion(instruccion)

        cv2.setMouseCallback(WIN_NAME, self._mouse_roi)

        while True:
            frame = img_ref.copy()
            _barra_superior(frame, f"ROI {preset_id}", self.preset_idx, NUM_PRESETS)

            # Dibujar polígono en curso
            if len(self._roi_pts_tmp) > 0:
                pts = np.array(self._roi_pts_tmp, dtype=np.int32)
                cv2.polylines(frame, [pts], isClosed=False, color=COL_ROI, thickness=2)
                if len(self._roi_pts_tmp) > 2:
                    # Línea de cierre (preview)
                    cv2.line(frame, tuple(self._roi_pts_tmp[-1]),
                             tuple(self._roi_pts_tmp[0]), COL_ROI, 1)
                for i, p in enumerate(self._roi_pts_tmp):
                    cv2.circle(frame, tuple(p), 5, COL_ROI, -1)
                    _text_shadow(frame, str(i + 1), (p[0] + 7, p[1] - 5),
                                 escala=0.50, color=COL_ROI)

            _barra_instruccion(frame, instruccion, frame.shape[0])
            cv2.imshow(WIN_NAME, frame)

            key = cv2.waitKey(30) & 0xFF
            if key == 27:
                return False
            elif key in (ord('z'), ord('Z')):
                if self._roi_pts_tmp:
                    self._roi_pts_tmp.pop()
                    self._instruccion(f"↩ Punto eliminado. Quedan {len(self._roi_pts_tmp)}.")
            elif key == 13:
                if len(self._roi_pts_tmp) < 3:
                    self._instruccion("⚠️  Necesitas al menos 3 puntos.")
                else:
                    self.roi_puntos = [list(p) for p in self._roi_pts_tmp]
                    self._pub_estado()
                    return True

    def _fase_ancla(self, preset_id: int, img_ref: np.ndarray) -> bool:
        """
        El operador arrastra un rectángulo sobre la imagen para definir el anclaje.
        ENTER confirma, ESC cancela.
        Coordenadas en píxeles REALES.
        """
        self.state = STATE_ANCLA
        self._ancla_tmp = []
        self._ancla_definida.clear()
        self._drag_start = None
        self._drag_end   = None
        self._pub_estado()
        instruccion = (f"[ANCLAJE {preset_id}/{NUM_PRESETS}]  "
                       f"ARRASTRE=seleccionar referencia fija  ENTER=confirmar  ESC=cancelar")
        self._instruccion(instruccion)

        cv2.setMouseCallback(WIN_NAME, self._mouse_ancla)

        while True:
            frame = img_ref.copy()
            _barra_superior(frame, f"ANCLAJE {preset_id}", self.preset_idx, NUM_PRESETS)

            # Preview del arrastre en curso
            if self._drag_start and self._drag_end:
                x0 = min(self._drag_start[0], self._drag_end[0])
                y0 = min(self._drag_start[1], self._drag_end[1])
                x1 = max(self._drag_start[0], self._drag_end[0])
                y1 = max(self._drag_start[1], self._drag_end[1])
                cv2.rectangle(frame, (x0, y0), (x1, y1), COL_ANCLA, 2)

            # Rectángulo confirmado
            if self._ancla_tmp:
                ax, ay, aw, ah = self._ancla_tmp
                cv2.rectangle(frame, (ax, ay), (ax + aw, ay + ah), COL_ANCLA, 2)
                _text_shadow(frame, "ANCLAJE", (ax + 4, ay - 8),
                             escala=0.55, color=COL_ANCLA)

            _barra_instruccion(frame, instruccion, frame.shape[0])
            cv2.imshow(WIN_NAME, frame)

            key = cv2.waitKey(30) & 0xFF
            if key == 27:
                return False
            elif key == 13:
                if not self._ancla_tmp:
                    self._instruccion("⚠️  Primero selecciona el rectángulo de anclaje.")
                else:
                    self.ancla_rect = list(self._ancla_tmp)
                    self._pub_estado({"ancla_rect": self.ancla_rect})
                    return True

    # ─────────────────────────────────────────────────────────────────────────
    # Worker principal
    # ─────────────────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        self.misiones    = []
        self.preset_idx  = 0
        self._curr_pan   = 0

        self._pub_bloqueado(True)
        self._log("INFO", "═══ INICIO DE CALIBRACIÓN (ventana OpenCV) ═══")

        # Crear ventana fullscreen
        cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WIN_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

        # HOME
        self.state = STATE_HOME
        self._pub_estado()

        # Mostrar pantalla de espera durante el HOME
        img_wait = self._snap()
        if img_wait is None:
            img_wait = np.zeros((540, 960, 3), dtype=np.uint8)
        _barra_instruccion(img_wait,
                           "Sincronizando HOME — espera (~15 s)...", img_wait.shape[0])
        cv2.imshow(WIN_NAME, img_wait)
        cv2.waitKey(1)

        self._home()

        for i in range(NUM_PRESETS):
            self.preset_idx = i
            preset_id       = i + 1

            # ── POSICIÓN ──────────────────────────────────────────────────────
            if not self._fase_posicion(preset_id):
                self._cancelar()
                return

            pan_guardado = self._curr_pan
            self._guardar_preset_camara(preset_id)
            self._log("INFO", f"✅ Posición {preset_id} confirmada (pan={pan_guardado}).")

            # Capturar imagen de referencia (se usa para ROI y ANCLAJE)
            img_ref = self._snap()
            if img_ref is None:
                self._log("ALARM", f"No se pudo capturar imagen para preset {preset_id}. Abortando.")
                self._cancelar()
                return

            # ── ROI ───────────────────────────────────────────────────────────
            if not self._fase_roi(preset_id, img_ref):
                self._cancelar()
                return

            roi_guardado = [list(p) for p in self.roi_puntos]
            self._log("INFO", f"✅ ROI {preset_id} confirmado ({len(roi_guardado)} puntos).")

            # ── ANCLAJE ───────────────────────────────────────────────────────
            if not self._fase_ancla(preset_id, img_ref):
                self._cancelar()
                return

            ax, ay, aw, ah = self.ancla_rect
            centro  = [ax + aw // 2, ay + ah // 2]

            # Recortar y guardar plantilla de referencia
            plantilla = img_ref[ay:ay + ah, ax:ax + aw]
            ref_path  = CALIB_DIR / f"ref_{preset_id}.png"
            cv2.imwrite(str(ref_path), plantilla)
            self._log("INFO", f"✅ Referencia guardada: {ref_path.name}  "
                              f"(centro real: {centro[0]},{centro[1]})  "
                              f"ROI: {len(roi_guardado)} pts")

            self.misiones.append({
                "id":      preset_id,
                "pan_abs": pan_guardado,
                "roi":     roi_guardado,
                "centro":  centro,
            })

        # ── Guardar config.json ───────────────────────────────────────────────
        config_path = CALIB_DIR / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.misiones, f, indent=4, ensure_ascii=False)

        self.state = STATE_DONE
        self._pub_estado()
        self._log("INFO", "═══ CALIBRACIÓN COMPLETADA ═══  config.json guardado.")
        self._instruccion("🎉 Calibración finalizada. Puedes iniciar la patrulla.")

        # Mostrar pantalla de éxito 3 s
        img_ok = self._snap() or np.zeros((540, 960, 3), dtype=np.uint8)
        _barra_instruccion(img_ok, "✅ CALIBRACIÓN COMPLETADA — cerrando ventana...", img_ok.shape[0])
        cv2.imshow(WIN_NAME, img_ok)
        cv2.waitKey(3000)

        cv2.destroyWindow(WIN_NAME)
        self._pub_bloqueado(False)
        time.sleep(1)
        self.state = STATE_IDLE
        self._pub_estado()

    def _cancelar(self) -> None:
        self._log("INFO", "Calibración cancelada.")
        cv2.destroyWindow(WIN_NAME)
        self.state = STATE_IDLE
        self._pub_estado()
        self._pub_bloqueado(False)

    # ─────────────────────────────────────────────────────────────────────────
    # API pública (llamada desde app.py)
    # ─────────────────────────────────────────────────────────────────────────

    def recibir_comando(self, payload: dict) -> None:
        """
        Solo se acepta 'iniciar' para lanzar el worker.
        El resto de la interacción (A/D, clics, ENTER) ocurre
        directamente en la ventana OpenCV, no por MQTT.
        """
        accion = payload.get("accion")

        if accion == "iniciar":
            if self._thread and self._thread.is_alive():
                self._log("INFO", "La calibración ya está en curso.")
                return
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

        elif accion == "cancelar":
            # Permite cancelar desde el botón web aunque la interacción
            # sea local (presionando ESC en la ventana)
            if self._thread and self._thread.is_alive():
                # Señalamos el cierre destruyendo la ventana;
                # el worker detectará el fallo en waitKey y saldrá.
                try:
                    cv2.destroyWindow(WIN_NAME)
                except Exception:
                    pass
                self.state = STATE_IDLE
                self._pub_estado()
                self._pub_bloqueado(False)