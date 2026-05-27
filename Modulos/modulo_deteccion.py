"""
modulos/modulo_deteccion.py — Detección de obstáculos con YOLOWorld.

Responsabilidades:
  - Suscribir a heliwarden/patrulla/captura (notificaciones de imagen lista).
  - Evaluar calidad de imagen (varianza Laplaciana).
  - Correr inferencia YOLOWorld con soporte de tiles solapados.
  - Detectar candidatos desconocidos por diferencia con la última referencia del preset.
  - Aplicar ROI por preset (polígono leído de calibracion/config.json).
  - Publicar resultado enriquecido en MQTT → heliwarden/deteccion:
      {"preset": int, "objetos": [...], "occupancy_status": str, "camera_ok": bool}

NO escribe en helipuertos.json directamente.
NO decide umbral de repeticiones (eso lo hace fusion_estados.py).

Corre como proceso independiente: python modulos/modulo_deteccion.py
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

try:
    import torch
    from torchvision.ops import nms as _torch_nms
except Exception:
    torch      = None   # type: ignore
    _torch_nms = None

# ── Paths ──────────────────────────────────────────────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

# ── Configuración ──────────────────────────────────────────────────────────────

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

CALIB_DIR   = ROOT_DIR / "calibracion"
FOTOS_DIR   = ROOT_DIR / "fotos presets"

MODELO_WORLD_PT = ROOT_DIR / "Datos" / "YOLO" / "yolov8l-worldv2.pt"

CONFIANZA        = float(os.getenv("YOLO_CONFIANZA_WORLD",  0.10))
IOU_THR          = float(os.getenv("YOLO_IOU",              0.45))
IMGSZ            = int(os.getenv("YOLO_IMGSZ",              640))
BLUR_THRESHOLD   = float(os.getenv("YOLO_BLUR_THRESHOLD",   80.0))
USE_TILES        = os.getenv("YOLO_USE_TILES", "true").lower() == "true"
TILE_SIZE        = int(os.getenv("YOLO_TILE_SIZE",          960))
TILE_OVERLAP     = float(os.getenv("YOLO_TILE_OVERLAP",     0.25))
UNKNOWN_MIN_AREA = int(os.getenv("YOLO_UNKNOWN_MIN_AREA",   400))
DIFF_THRESHOLD   = int(os.getenv("YOLO_DIFF_THRESHOLD",     35))

TOPIC_CAPTURA   = "heliwarden/patrulla/captura"
TOPIC_DETECCION = "heliwarden/deteccion"

DEBUG_DIR       = ROOT_DIR / "debug_detecciones"
MAX_IMGS_PRESET = 3

# ── Clases YOLOWorld — contexto de oficina ────────────────────────────────────
# Adaptadas para pruebas con imágenes de oficina.
# En producción (helipuerto) ampliar con HELIPAD_EXTRA_CLASSES del script de inferencia.

WORLD_CLASSES = sorted({
    # Personas
    "person", "employee", "visitor",

    # Mobiliario
    "desk", "office chair", "chair", "table", "bookshelf", "cabinet",
    "sofa", "armchair", "filing cabinet", "shelf", "drawer", "partition",

    # Tecnología
    "laptop", "computer monitor", "keyboard", "mouse", "phone",
    "tablet", "printer", "scanner", "projector", "router", "server rack",
    "power strip", "cable", "extension cord",

    # Papelería / objetos pequeños
    "notebook", "binder", "book", "pen", "pencil", "stapler",
    "mug", "cup", "bottle", "water bottle",

    # Bolsas / contenedores
    "backpack", "handbag", "bag", "box", "cardboard box", "suitcase",
    "trash can", "waste bin",

    # Seguridad / emergencia
    "fire extinguisher", "wet floor sign", "first aid kit",

    # Genérico / anomalías
    "obstacle", "foreign object", "unknown object",
})

# ── Estado global ──────────────────────────────────────────────────────────────

_world_model: object    = None
_config_presets: dict   = {}


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
    _world_model.set_classes(list(WORLD_CLASSES))
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


# ── Calidad de imagen ─────────────────────────────────────────────────────────

def _blur_score(image: np.ndarray) -> float:
    """Varianza del Laplaciano: valores bajos indican imagen borrosa o plana."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _check_quality(image: np.ndarray) -> tuple[bool, float]:
    """Devuelve (camera_ok, score)."""
    score = _blur_score(image)
    return score >= BLUR_THRESHOLD, score


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


# ── Tiles ─────────────────────────────────────────────────────────────────────

def _generate_tiles(width: int, height: int, tile_size: int, overlap: float) -> list[tuple]:
    """Devuelve lista de (x1, y1, x2, y2) cubriendo toda la imagen."""
    if tile_size <= 0:
        return []
    stride = max(1, int(tile_size * (1.0 - overlap)))
    xs = list(range(0, max(1, width - tile_size + 1), stride))
    ys = list(range(0, max(1, height - tile_size + 1), stride))
    if not xs or xs[-1] + tile_size < width:
        xs.append(max(0, width - tile_size))
    if not ys or ys[-1] + tile_size < height:
        ys.append(max(0, height - tile_size))
    tiles = []
    for y in ys:
        for x in xs:
            tiles.append((x, y, min(width, x + tile_size), min(height, y + tile_size)))
    return tiles


# ── NMS ───────────────────────────────────────────────────────────────────────

def _box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1 = np.maximum(a[0], b[:, 0])
    y1 = np.maximum(a[1], b[:, 1])
    x2 = np.minimum(a[2], b[:, 2])
    y2 = np.minimum(a[3], b[:, 3])
    inter  = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = np.maximum(0, b[:, 2] - b[:, 0]) * np.maximum(0, b[:, 3] - b[:, 1])
    return inter / (area_a + area_b - inter + 1e-9)


def _python_nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        ious  = _box_iou(boxes[i], boxes[order[1:]])
        order = order[1:][ious <= iou_thr]
    return keep


def _merge_detections(dets: list[dict], iou_thr: float = 0.50, class_aware: bool = True) -> list[dict]:
    """NMS sobre lista de dicts con clave '_box'. Devuelve lista filtrada."""
    if not dets:
        return []
    groups: dict[str, list[int]] = {}
    for idx, d in enumerate(dets):
        key = d["clase"] if class_aware else "all"
        groups.setdefault(key, []).append(idx)

    output: list[dict] = []
    for indices in groups.values():
        boxes  = np.array([dets[i]["_box"] for i in indices], dtype=np.float32)
        scores = np.array([dets[i]["confianza"] for i in indices], dtype=np.float32)
        if _torch_nms is not None and torch is not None:
            keep = _torch_nms(
                torch.tensor(boxes, dtype=torch.float32),
                torch.tensor(scores, dtype=torch.float32),
                iou_thr,
            ).cpu().numpy().tolist()
        else:
            keep = _python_nms(boxes, scores, iou_thr)
        for k in keep:
            output.append(dets[indices[int(k)]])
    return sorted(output, key=lambda d: d["confianza"], reverse=True)


# ── Referencia — última foto del preset ───────────────────────────────────────

def _cargar_referencia_anterior(id_preset: int, ruta_actual: str) -> Optional[np.ndarray]:
    """
    Busca en 'fotos presets/' la penúltima imagen del preset (la anterior a la actual),
    para no comparar la imagen consigo misma.
    Si solo existe una foto (la actual), no hay referencia disponible.
    """
    patron  = f"{id_preset}_*.png"
    fotos   = sorted(FOTOS_DIR.glob(patron), key=lambda p: p.stat().st_mtime)
    # Excluir la imagen que acaba de capturarse
    fotos   = [f for f in fotos if str(f) != ruta_actual]
    if not fotos:
        return None
    ref = cv2.imread(str(fotos[-1]))
    if ref is not None:
        print(f"  📎 Referencia anterior: {fotos[-1].name}")
    return ref


# ── Candidatos desconocidos por diferencia ────────────────────────────────────

def _foreground_candidates(
    image: np.ndarray,
    reference: Optional[np.ndarray],
    known_boxes: list[list[float]],
    roi_mask: Optional[np.ndarray],
) -> list[dict]:
    """
    Compara image con reference y devuelve regiones que cambian
    y no quedan explicadas por ninguna detección YOLO.
    """
    if reference is None:
        return []

    if reference.shape[:2] != image.shape[:2]:
        reference = cv2.resize(reference, (image.shape[1], image.shape[0]))

    gray = cv2.cvtColor(image,     cv2.COLOR_BGR2GRAY)
    ref  = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(gray, ref)
    diff = cv2.GaussianBlur(diff, (5, 5), 0)
    _, mask = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

    if roi_mask is not None:
        mask = cv2.bitwise_and(mask, roi_mask)

    kernel = np.ones((5, 5), dtype=np.uint8)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    known = np.array(known_boxes, dtype=np.float32) if known_boxes else np.zeros((0, 4), dtype=np.float32)

    candidates: list[dict] = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < UNKNOWN_MIN_AREA:
            continue
        x, y, w, h = cv2.boundingRect(c)
        box = np.array([x, y, x + w, y + h], dtype=np.float32)

        if known.size > 0:
            max_iou = float(_box_iou(box, known).max())
            if max_iou > 0.20:
                continue  # ya explicado por YOLO

        score = min(1.0, float(area) / 5000.0)
        cx    = round(float(x + w / 2), 1)
        cy    = round(float(y + h / 2), 1)
        print(f"    ❓ unknown_object_candidate — área={area:.0f}  ({cx:.0f}, {cy:.0f})")
        candidates.append({
            "clase":     "unknown_object_candidate",
            "cx":        cx,
            "cy":        cy,
            "confianza": round(score, 3),
            "_box":      box.tolist(),
        })
    return candidates


# ── Run YOLO con tiles ────────────────────────────────────────────────────────

def _run_yolo_tiles(
    image: np.ndarray,
    roi_mask: Optional[np.ndarray],
) -> list[dict]:
    """
    Ejecuta YOLOWorld sobre la imagen completa y, si USE_TILES,
    también sobre teselas solapadas. Aplica NMS global al final.
    """
    if _world_model is None:
        return []

    h, w    = image.shape[:2]
    windows = [(0, 0, w, h, "full")]
    if USE_TILES:
        tiles = _generate_tiles(w, h, TILE_SIZE, TILE_OVERLAP)
        windows += [(x1, y1, x2, y2, "tile") for x1, y1, x2, y2 in tiles]
        print(f"  🔲 Tiles: {len(tiles)} recortes + imagen completa")

    dets: list[dict] = []
    names = _world_model.names

    for x1, y1, x2, y2, src in windows:
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        try:
            results = _world_model.predict(
                crop,
                imgsz=IMGSZ,
                conf=CONFIANZA,
                iou=IOU_THR,
                verbose=False,
            )
        except Exception as e:
            print(f"  ⚠️  Error en inferencia ({src}): {e}")
            continue

        for r in results:
            if r.boxes is None:
                continue
            for box, cls_id, score in zip(
                r.boxes.xyxy.cpu().numpy(),
                r.boxes.cls.cpu().numpy().astype(int),
                r.boxes.conf.cpu().numpy(),
            ):
                # Recomponer coordenadas globales desde el tile
                global_box = [
                    float(box[0]) + x1,
                    float(box[1]) + y1,
                    float(box[2]) + x1,
                    float(box[3]) + y1,
                ]
                if not _center_inside_roi(global_box, roi_mask):
                    continue
                label = (
                    str(names.get(int(cls_id), cls_id))
                    if isinstance(names, dict)
                    else str(names[int(cls_id)])
                )
                cx  = round((global_box[0] + global_box[2]) / 2, 1)
                cy  = round((global_box[1] + global_box[3]) / 2, 1)
                pct = round(float(score) * 100, 1)
                print(f"    🎯 [{src}] {label} — {pct}%  |  ({cx:.0f}, {cy:.0f})")
                dets.append({
                    "clase":     label,
                    "cx":        cx,
                    "cy":        cy,
                    "confianza": round(float(score), 3),
                    "_box":      global_box,
                })

    # NMS global (elimina duplicados entre tiles)
    dets = _merge_detections(dets, iou_thr=IOU_THR, class_aware=True)
    return dets


# ── Debug ─────────────────────────────────────────────────────────────────────

def _guardar_debug_imagen(
    id_preset: int,
    image: np.ndarray,
    yolo_dets: list[dict],
    unknown_dets: list[dict],
    roi_mask: Optional[np.ndarray],
) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        out = image.copy()

        # Dibujar ROI
        if roi_mask is not None:
            roi_contours, _ = cv2.findContours(roi_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(out, roi_contours, -1, (0, 120, 255), 2)

        # Detecciones YOLO → verde
        for d in yolo_dets:
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

        # Candidatos desconocidos → naranja
        for d in unknown_dets:
            box = d.get("_box")
            if not box:
                continue
            x1, y1, x2, y2 = map(int, box)
            label = f"? {d['confianza']:.2f}"
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 140, 255), 2)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(out, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 140, 255), -1)
            cv2.putText(out, label, (x1 + 2, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath  = DEBUG_DIR / f"preset{id_preset}_{timestamp}.jpg"
        cv2.imwrite(str(filepath), out, [cv2.IMWRITE_JPEG_QUALITY, 85])

        # Rotar: mantener solo las últimas MAX_IMGS_PRESET imágenes por preset
        existentes = sorted(
            DEBUG_DIR.glob(f"preset{id_preset}_*.jpg"),
            key=lambda p: p.stat().st_mtime,
        )
        for viejo in existentes[:-MAX_IMGS_PRESET]:
            viejo.unlink(missing_ok=True)

        print(f"  📸 Debug guardado: {filepath.name}")
    except Exception as e:
        print(f"  ⚠️  Error guardando debug: {e}")


# ── Pipeline principal ─────────────────────────────────────────────────────────

def analizar_imagen(id_preset: int, ruta_imagen: str) -> dict:
    """
    Devuelve un dict con:
      {
        "objetos":          [...],   # detecciones YOLO + desconocidos, sin _box
        "occupancy_status": str,     # LIBRE | OCUPADO_OBJETO | OCUPADO_HELICOPTERO
        "camera_ok":        bool,
        "blur_score":       float,
      }
    """
    print(f"\n🔍 Analizando preset {id_preset}: {ruta_imagen}")

    image = cv2.imread(ruta_imagen)
    if image is None:
        print(f"  ❌ No se pudo leer la imagen: {ruta_imagen}")
        return {"objetos": [], "occupancy_status": "LIBRE", "camera_ok": False, "blur_score": 0.0}

    # ── 1. Calidad de imagen ──────────────────────────────────────────────────
    camera_ok, blur_score = _check_quality(image)
    if not camera_ok:
        print(f"  ⚠️  Imagen con baja nitidez (score={blur_score:.1f} < {BLUR_THRESHOLD}). "
              f"Detección degradada.")
    else:
        print(f"  ✅ Calidad OK (blur_score={blur_score:.1f})")

    if _world_model is None:
        print("  ❌ Modelo no cargado.")
        return {"objetos": [], "occupancy_status": "LIBRE", "camera_ok": camera_ok, "blur_score": blur_score}

    # ── 2. ROI ────────────────────────────────────────────────────────────────
    roi_mask: Optional[np.ndarray] = None
    roi_poly = _config_presets.get(id_preset, {}).get("roi")
    if roi_poly:
        roi_mask = _make_roi_mask(image.shape[:2], roi_poly)

    # ── 3. Inferencia YOLO con tiles ──────────────────────────────────────────
    yolo_dets = _run_yolo_tiles(image, roi_mask)

    # ── 4. Candidatos desconocidos por diferencia con referencia anterior ─────
    referencia   = _cargar_referencia_anterior(id_preset, ruta_imagen)
    known_boxes  = [d["_box"] for d in yolo_dets]
    unknown_dets = _foreground_candidates(image, referencia, known_boxes, roi_mask)

    # ── 5. Estado de ocupación ────────────────────────────────────────────────
    tiene_helicoptero = any(
        d["clase"].lower() in ("helicopter", "helicoptero") for d in yolo_dets
    )
    if tiene_helicoptero:
        occupancy_status = "OCUPADO_HELICOPTERO"
    elif yolo_dets or unknown_dets:
        occupancy_status = "OCUPADO_OBJETO"
    else:
        occupancy_status = "LIBRE"

    all_dets = yolo_dets + unknown_dets
    print(f"  → YOLO: {len(yolo_dets)} det.  Desconocidos: {len(unknown_dets)}  "
          f"Estado: {occupancy_status}")

    # ── 6. Debug ──────────────────────────────────────────────────────────────
    _guardar_debug_imagen(id_preset, image, yolo_dets, unknown_dets, roi_mask)

    # Limpiar _box interno antes de publicar
    objetos = [
        {
            "clase":     d["clase"],
            "cx":        d["cx"],
            "cy":        d["cy"],
            "confianza": d["confianza"],
        }
        for d in all_dets
    ]

    return {
        "objetos":          objetos,
        "occupancy_status": occupancy_status,
        "camera_ok":        camera_ok,
        "blur_score":       round(blur_score, 1),
    }


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

        resultado = analizar_imagen(id_preset, ruta)

        # Publicar resultado enriquecido
        mqtt_payload = json.dumps({
            "preset":           id_preset,
            "objetos":          resultado["objetos"],
            "occupancy_status": resultado["occupancy_status"],
            "camera_ok":        resultado["camera_ok"],
            "blur_score":       resultado["blur_score"],
        })
        client.publish(TOPIC_DETECCION, mqtt_payload, qos=1)
        print(f"  → Publicado: {len(resultado['objetos'])} obj, "
              f"estado={resultado['occupancy_status']}, camera_ok={resultado['camera_ok']}")

        # ACK al módulo de patrulla
        client.publish(
            "heliwarden/deteccion/ack",
            json.dumps({"preset": id_preset}),
            qos=1,
        )

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