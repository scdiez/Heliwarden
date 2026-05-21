"""
modulos/modulo_deteccion.py — Detección de obstáculos con YOLOWorld.

Responsabilidades:
  - Suscribir a heliwarden/patrulla/captura (notificaciones de imagen lista).
  - Correr inferencia con YOLOWorld sobre la imagen completa (sin tiles).
  - Aplicar ROI por preset (polígono leído de calibracion/config.json).
  - Publicar objetos detectados en MQTT → heliwarden/deteccion.

NO usa tiles.
NO compara con imagen de referencia.
NO escribe en helipuertos.json directamente.
NO decide si hay obstáculo (eso lo hace fusion_estados.py).

Corre como proceso independiente: `python modulos/modulo_deteccion.py`
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# ── Paths basados en la ubicación de este archivo ─────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

# ── Configuración ─────────────────────────────────────────────────────────────

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

CALIB_DIR = ROOT_DIR / "calibracion"

MODELO_WORLD_PT = Path(os.getenv(
    "YOLO_WORLD_MODEL",
    str(ROOT_DIR.parent / "TESTUNIFICACION" / "yolov8l-worldv2.pt")
))
CONFIANZA = float(os.getenv("YOLO_CONFIANZA_WORLD", 0.1))
IOU_THR   = float(os.getenv("YOLO_IOU", 0.45))
IMGSZ     = int(os.getenv("YOLO_IMGSZ", 640))

TOPIC_CAPTURA   = "heliwarden/patrulla/captura"
TOPIC_DETECCION = "heliwarden/deteccion"

DEBUG_DIR       = ROOT_DIR / "debug_detecciones"
MAX_IMGS_PRESET = 3

WORLD_CLASSES = [
    "person", "employee", "visitor",
    "desk", "office chair", "table", "bookshelf", "cabinet",
    "laptop", "computer monitor", "keyboard", "mouse", "phone",
    "printer", "projector", "water dispenser",
    "notebook", "binder", "pen", "mug", "bottle",
    "backpack", "handbag", "trash can",
    "cable", "power strip", "fire extinguisher", "wet floor sign",
]

# ── Estado global ─────────────────────────────────────────────────────────────

_world_model    = None
_config_presets: dict = {}


# ── Carga de modelo y config ──────────────────────────────────────────────────

def _load_model() -> None:
    global _world_model
    try:
        from ultralytics import YOLOWorld
    except ImportError:
        print("⚠️  ultralytics no instalado. Instala con: pip install ultralytics")
        return

    if not MODELO_WORLD_PT.exists():
        print(f"⚠️  Modelo YOLOWorld no encontrado: {MODELO_WORLD_PT}")
        return

    print(f"  📦 Cargando YOLOWorld: {MODELO_WORLD_PT}")
    _world_model = YOLOWorld(str(MODELO_WORLD_PT))
    _world_model.set_classes(WORLD_CLASSES)
    print(f"  ✅ Modelo cargado con {len(WORLD_CLASSES)} clases.")


def _load_config() -> None:
    global _config_presets
    config_path = CALIB_DIR / "config.json"
    if not config_path.exists():
        print(f"  ⚠️  No se encontró {config_path}; sin ROI por preset.")
        return
    with open(config_path, "r", encoding="utf-8") as f:
        lista = json.load(f)
    for entry in lista:
        _config_presets[entry["id"]] = entry
    print(f"  ✅ Config de {len(_config_presets)} presets cargada.")


# ── ROI ───────────────────────────────────────────────────────────────────────

def _make_roi_mask(shape_hw: tuple, polygon: list) -> np.ndarray:
    h, w = shape_hw
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [np.array(polygon, dtype=np.int32)], 255)
    return mask


def _center_inside_roi(box_xyxy: list, roi_mask: Optional[np.ndarray]) -> bool:
    if roi_mask is None:
        return True
    x1, y1, x2, y2 = box_xyxy
    cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
    h, w = roi_mask.shape[:2]
    if cx < 0 or cy < 0 or cx >= w or cy >= h:
        return False
    return roi_mask[cy, cx] > 0


# ── Debug ─────────────────────────────────────────────────────────────────────

def _guardar_debug_imagen(id_preset: int, image: np.ndarray, dets: list) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        out = image.copy()
        for d in dets:
            box = d.get("_box")
            if not box:
                continue
            x1, y1, x2, y2 = map(int, box)
            label = f"{d['clase']} {d['confianza']:.2f}"
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 220, 0), 2)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(out, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 220, 0), -1)
            cv2.putText(out, label, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath  = DEBUG_DIR / f"preset{id_preset}_{timestamp}.jpg"
        cv2.imwrite(str(filepath), out, [cv2.IMWRITE_JPEG_QUALITY, 85])

        existentes = sorted(
            DEBUG_DIR.glob(f"preset{id_preset}_*.jpg"),
            key=lambda p: p.stat().st_mtime,
        )
        for viejo in existentes[:-MAX_IMGS_PRESET]:
            viejo.unlink(missing_ok=True)

        print(f"  📸 Debug guardado: {filepath.name}")
    except Exception as e:
        print(f"  ⚠️  Error guardando debug: {e}")


# ── Pipeline de análisis ──────────────────────────────────────────────────────

def analizar_imagen(id_preset: int, ruta_imagen: str) -> list:
    print(f"\n🔍 Analizando preset {id_preset}: {ruta_imagen}")

    image = cv2.imread(ruta_imagen)
    if image is None:
        print(f"  ❌ No se pudo leer la imagen: {ruta_imagen}")
        return []

    if _world_model is None:
        print("  ❌ Modelo no cargado.")
        return []

    roi_mask: Optional[np.ndarray] = None
    roi_poly = _config_presets.get(id_preset, {}).get("roi")
    if roi_poly:
        roi_mask = _make_roi_mask(image.shape[:2], roi_poly)

    try:
        results = _world_model.predict(
            image,
            imgsz=IMGSZ,
            conf=CONFIANZA,
            iou=IOU_THR,
            verbose=False,
        )
    except Exception as e:
        print(f"  ❌ Error en inferencia: {e}")
        return []

    dets  = []
    names = _world_model.names
    for r in results:
        if r.boxes is None:
            continue
        for box, cls_id, score in zip(
            r.boxes.xyxy.cpu().numpy(),
            r.boxes.cls.cpu().numpy().astype(int),
            r.boxes.conf.cpu().numpy(),
        ):
            box_list = [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
            if not _center_inside_roi(box_list, roi_mask):
                continue
            label = str(names.get(int(cls_id), cls_id)) if isinstance(names, dict) else str(names[int(cls_id)])
            cx  = round((box_list[0] + box_list[2]) / 2, 1)
            cy  = round((box_list[1] + box_list[3]) / 2, 1)
            pct = round(float(score) * 100, 1)
            print(f"    🎯 {label} — {pct}%  |  ({cx:.0f}, {cy:.0f})")
            dets.append({
                "clase":     label,
                "cx":        cx,
                "cy":        cy,
                "confianza": round(float(score), 3),
                "_box":      box_list,
            })

    if not dets:
        print("    ✅ Sin detecciones.")

    print(f"  → Total detecciones: {len(dets)}")
    _guardar_debug_imagen(id_preset, image, dets)

    return [
        {"clase": d["clase"], "cx": d["cx"], "cy": d["cy"], "confianza": d["confianza"]}
        for d in dets
    ]


# ── MQTT callbacks ────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ modulo_deteccion conectado al broker MQTT.")
        client.subscribe(TOPIC_CAPTURA, qos=1)
    else:
        print(f"❌ modulo_deteccion: fallo MQTT (rc={rc})")


def _on_message(client, userdata, msg):
    try:
        payload   = json.loads(msg.payload.decode())
        id_preset = payload.get("preset")
        ruta      = payload.get("ruta")

        if not ruta or not Path(ruta).exists():
            print(f"[modulo_deteccion] Ruta no válida: {ruta}")
            return

        objetos   = analizar_imagen(id_preset, ruta)
        resultado = json.dumps({"preset": id_preset, "objetos": objetos})
        client.publish(TOPIC_DETECCION, resultado, qos=1)
        print(f"  → Publicados {len(objetos)} objetos para preset {id_preset}")

        client.publish("heliwarden/deteccion/ack",
                       json.dumps({"preset": id_preset}), qos=1)

    except Exception as e:
        print(f"[modulo_deteccion] Error procesando captura: {e}")


def _on_disconnect(client, userdata, rc):
    print(f"⚠️  modulo_deteccion desconectado del broker (rc={rc}). Reintentando...")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Cargando modelo y config...")
    _load_config()
    _load_model()

    client = mqtt.Client(client_id="heliwarden-deteccion")
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    print(f"Conectando a broker MQTT en {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nmodulo_deteccion detenido.")
        client.disconnect()
