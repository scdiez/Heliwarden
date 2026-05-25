"""
app.py — Servidor Flask.
Responsabilidad única: servir el frontend y los datos de helipuertos.json.
No contiene lógica de negocio, no mueve la cámara, no analiza imágenes.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, make_response, render_template

import cv2
import time

# ── Paths basados en la ubicación de este archivo ─────────────────────────────
ROOT_DIR    = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT_DIR / "templates"

# Añadir Modulos/ al path para importar los módulos del sistema
_modulos_dir = str(ROOT_DIR / "Modulos")
if _modulos_dir not in sys.path:
    sys.path.insert(0, _modulos_dir)

from modulo_calibracion import Calibrador, STATE_IDLE  # type: ignore[import]

load_dotenv(ROOT_DIR / ".env")

DATA_FILE = ROOT_DIR / "helipuertos.json"

USER     = os.getenv("CAMERA_USER")
PASS     = os.getenv("CAMERA_PASS")
IP       = os.getenv("CAMERA_IP")
RTSP_URL = f"rtsp://{USER}:{PASS}@{IP}:554/videoSub"

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))

# ── Persistencia ──────────────────────────────────────────────────────────────

_file_lock = threading.Lock()

def load_data() -> dict:
    with _file_lock:
        if not DATA_FILE.exists():
            return {"config": {}, "logs": []}
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

def save_data(data: dict) -> None:
    with _file_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


# ── MQTT ──────────────────────────────────────────────────────────────────────

_mensajes_pendientes: list = []
_mqtt_lock = threading.Lock()

def _on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic   = msg.topic

        if topic == "heliwarden/log":
            with _mqtt_lock:
                _mensajes_pendientes.append(payload)

        elif topic == "heliwarden/cmd/patrulla":
            accion = payload.get("accion")
            if accion in ("iniciar", "detener"):
                client.publish(
                    "heliwarden/patrulla/cmd",
                    json.dumps({"accion": accion}),
                    qos=1,
                )

        elif topic == "heliwarden/calibracion/cmd":
            if _calibrador is not None:
                _calibrador.recibir_comando(payload)

    except Exception as e:
        print(f"[app.py] Error procesando mensaje MQTT: {e}")


_mqtt_client = mqtt.Client(client_id="heliwarden-app")
_mqtt_client.on_message = _on_mqtt_message

_calibrador: Calibrador | None = None

def _iniciar_mqtt():
    global _calibrador
    try:
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        _mqtt_client.subscribe([
            ("heliwarden/log", 1),
            ("heliwarden/cmd/patrulla", 1),
            ("heliwarden/calibracion/cmd", 1),
        ])
        _mqtt_client.loop_start()
        print("✅ app.py conectado al broker MQTT.")
        _calibrador = Calibrador(_mqtt_client)
        print("✅ Calibrador inicializado.")
    except Exception as e:
        print(f"⚠️  app.py no pudo conectar al broker MQTT: {e}")


# ── Stream de vídeo ───────────────────────────────────────────────────────────

class VideoStream:
    def __init__(self, src: str):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.src     = src
        self.cap     = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        self.frame   = None
        self.ret     = False
        self.stopped = False

    def start(self):
        threading.Thread(target=self._update, daemon=True).start()
        return self

    def _update(self):
        while not self.stopped:
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.src, cv2.CAP_FFMPEG)
                time.sleep(2)
                continue
            ret, frame = self.cap.read()
            if ret:
                self.frame = frame
                self.ret   = True
            else:
                self.ret = False
                self.cap.release()
                time.sleep(0.5)

    def get_frame(self):
        return self.frame if self.ret else None


stream = VideoStream(RTSP_URL).start()


def _generate_frames(mode: str):
    while True:
        frame = stream.get_frame()
        if frame is None:
            time.sleep(0.1)
            continue
        try:
            if mode == "hq":
                frame = cv2.resize(frame, (1024, 576))
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                time.sleep(1)
            else:
                frame = cv2.resize(frame, (640, 360))
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 35])
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                time.sleep(0.04)
        except Exception:
            continue


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    data = load_data()
    return render_template("index.html", config=data["config"], logs=data["logs"])


@app.route("/video_feed/<mode>")
def video_feed(mode):
    if mode not in ("hq", "lq"):
        mode = "lq"
    return Response(_generate_frames(mode), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/ptz/<comando>")
def control_ptz(comando):
    if comando == "patrulla":
        _mqtt_client.publish("heliwarden/patrulla/cmd", json.dumps({"accion": "iniciar"}), qos=1)
        return jsonify({"status": "Comando de inicio enviado"})
    elif comando == "stop":
        _mqtt_client.publish("heliwarden/patrulla/cmd", json.dumps({"accion": "detener"}), qos=1)
        return jsonify({"status": "Comando de parada enviado"})
    return jsonify({"status": "Comando no reconocido"}), 400


@app.route("/ptz/mensajes")
def obtener_mensajes():
    with _mqtt_lock:
        mensajes = _mensajes_pendientes.copy()
        _mensajes_pendientes.clear()
    resultado = []
    for m in mensajes:
        nivel   = m.get("nivel", "INFO")
        mensaje = m.get("mensaje", "")
        if nivel == "ALARM":
            prefijo = "[ALARM]"
        elif nivel == "CALIB":
            prefijo = "[CALIB]"
        else:
            prefijo = "[INFO]"
        resultado.append(f"{prefijo} {mensaje}")
    return jsonify({"mensajes": resultado})


@app.route("/helipuertos")
def get_helipuertos():
    data = load_data()
    return jsonify(data.get("config", {}))


@app.route("/get_info/<id>")
def get_info(id):
    data = load_data()
    heli = data.get("config", {}).get(id)
    if heli:
        return jsonify(heli)
    return jsonify({"error": "Helipuerto no encontrado"}), 404


@app.route("/clear_logs", methods=["POST"])
def clear_logs():
    data = load_data()
    data["logs"] = []
    save_data(data)
    return jsonify({"status": "ok"})


@app.route("/export_logs")
def export_logs():
    data = load_data()
    lineas = [
        f"[{log['timestamp']}] [{log['nivel']}] {log['mensaje']}"
        for log in data.get("logs", [])
    ]
    response = make_response("\n".join(lineas))
    response.headers["Content-Disposition"] = "attachment; filename=log.txt"
    return response


@app.route("/status")
def status():
    return jsonify({
        "stream_rtsp": "activo" if stream.ret else "sin señal",
        "mqtt":        "conectado" if _mqtt_client.is_connected() else "desconectado",
    })


# ── Calibración ───────────────────────────────────────────────────────────────

@app.route("/calibracion/cmd", methods=["POST"])
def calibracion_cmd():
    """Recibe comandos de calibración del frontend y los publica en MQTT."""
    from flask import request
    payload = request.get_json(force=True) or {}
    _mqtt_client.publish(
        "heliwarden/calibracion/cmd",
        json.dumps(payload),
        qos=1,
    )
    return jsonify({"status": "ok"})


@app.route("/calibracion/estado")
def calibracion_estado():
    """Devuelve el estado actual del calibrador."""
    if _calibrador is None:
        return jsonify({"state": "idle", "preset": 1, "total": 3, "roi_puntos": []})
    return jsonify({
        "state":      _calibrador.state,
        "preset":     _calibrador.preset_idx + 1,
        "total":      3,
        "roi_puntos": _calibrador.roi_puntos,
        "ancla_rect": _calibrador.ancla_rect,
    })


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _iniciar_mqtt()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)