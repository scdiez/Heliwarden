import cv2
import os
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

def guardar_captura_hd(id_preset):
    # Configuración desde el .env
    USER = os.getenv("CAMERA_USER")
    PASS = os.getenv("CAMERA_PASS")
    IP = os.getenv("CAMERA_IP")
    # Usamos el canal principal (videoMain) para máxima resolución
    RTSP_URL = f"rtsp://{USER}:{PASS}@{IP}:554/videoMain"
    
    folder = "fotos presets"
    if not os.path.exists(folder):
        os.makedirs(folder)
    
    # Nombre del archivo: idpreset_timestamp.png
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{id_preset}_{timestamp}.png"
    filepath = os.path.join(folder, filename)
    
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        return None

    # Leer un par de frames para limpiar el buffer y obtener una imagen actual
    for _ in range(5):
        ret, frame = cap.read()
    
    if ret:
        # Rotamos si es necesario (según tu configuración anterior)
        frame = cv2.rotate(frame, cv2.ROTATE_180)
        cv2.imwrite(filepath, frame)
        cap.release()
        return filepath
    
    cap.release()
    return None