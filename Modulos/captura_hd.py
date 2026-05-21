import cv2
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ── Paths basados en la ubicación de este archivo ─────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

def guardar_captura_hd(id_preset):
    USER = os.getenv("CAMERA_USER")
    PASS = os.getenv("CAMERA_PASS")
    IP   = os.getenv("CAMERA_IP")
    RTSP_URL = f"rtsp://{USER}:{PASS}@{IP}:554/videoMain"

    folder = ROOT_DIR / "fotos presets"
    folder.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"{id_preset}_{timestamp}.png"
    filepath  = folder / filename

    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        return None

    for _ in range(5):
        ret, frame = cap.read()

    if ret:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
        cv2.imwrite(str(filepath), frame)
        cap.release()
        return str(filepath)

    cap.release()
    return None
