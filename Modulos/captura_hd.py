import cv2
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

# Umbral mínimo de varianza Laplaciana para considerar una imagen válida
UMBRAL_CALIDAD = float(os.getenv("CAPTURA_UMBRAL_CALIDAD", 300.0))
MAX_INTENTOS   = int(os.getenv("CAPTURA_MAX_INTENTOS", 5))
ESPERA_REINTENTO = 2.0  # segundos entre reintentos


def _es_imagen_valida(frame) -> tuple[bool, float]:
    """Devuelve (valida, varianza). Una imagen gris/corrupta tiene varianza muy baja."""
    gris = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    varianza = cv2.Laplacian(gris, cv2.CV_64F).var()
    return varianza >= UMBRAL_CALIDAD, varianza


def guardar_captura_hd(id_preset) -> str | None:
    USER = os.getenv("CAMERA_USER")
    PASS = os.getenv("CAMERA_PASS")
    IP   = os.getenv("CAMERA_IP")
    RTSP_URL = f"rtsp://{USER}:{PASS}@{IP}:554/videoMain"

    folder = ROOT_DIR / "fotos presets"
    folder.mkdir(exist_ok=True)

    for intento in range(1, MAX_INTENTOS + 1):
        print(f"   [Captura HD] Preset {id_preset} — intento {intento}/{MAX_INTENTOS}")

        cap = cv2.VideoCapture(RTSP_URL)
        if not cap.isOpened():
            print(f"   [Captura HD] No se pudo abrir el stream RTSP (intento {intento})")
            cap.release()
            time.sleep(ESPERA_REINTENTO)
            continue

        frame = None
        for _ in range(5):
            ret, f = cap.read()
            if ret:
                frame = f
        cap.release()

        if frame is None:
            print(f"   [Captura HD] No se recibió frame (intento {intento})")
            time.sleep(ESPERA_REINTENTO)
            continue

        frame = cv2.rotate(frame, cv2.ROTATE_180)
        valida, varianza = _es_imagen_valida(frame)

        if not valida:
            print(f"   [Captura HD] ⚠️  Imagen gris/corrupta descartada "
                  f"(varianza={varianza:.0f}, umbral={UMBRAL_CALIDAD:.0f}) — reintentando...")
            time.sleep(ESPERA_REINTENTO)
            continue

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath  = folder / f"{id_preset}_{timestamp}.png"
        cv2.imwrite(str(filepath), frame)
        print(f"   [Captura HD] ✅ Imagen válida guardada (varianza={varianza:.0f}): {filepath.name}")
        return str(filepath)

    print(f"   [Captura HD] ❌ No se obtuvo imagen válida tras {MAX_INTENTOS} intentos para preset {id_preset}")
    return None