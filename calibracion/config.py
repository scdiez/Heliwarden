import os, cv2, time, json, requests
import numpy as np
from onvif import ONVIFCamera
from dotenv import load_dotenv
from pathlib import Path

# --- CONFIGURACIÓN ---
load_dotenv()
BASE_DIR = Path(__file__).parent
CALIB_DIR = BASE_DIR 
CONFIG_FILE = CALIB_DIR / "config.json"

USER = os.getenv("CAMERA_USER")
PASS = os.getenv("CAMERA_PASS")
IP = os.getenv("CAMERA_IP")
ONVIF_PORT = int(os.getenv("CAMERA_ONVIF_PORT"))
HTTP_PORT = int(os.getenv("CAMERA_HTTP_PORT"))

HTTP_SNAP_URL = f"http://{IP}:{HTTP_PORT}/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2&usr={USER}&pwd={PASS}"

class ConfiguradorCompleto:
    def __init__(self):
        self.mycam = ONVIFCamera(IP, ONVIF_PORT, USER, PASS)
        self.ptz = self.mycam.create_ptz_service()
        self.token = self.mycam.create_media_service().GetProfiles()[0].token
        self.curr_pan = 0
        self.misiones = []
        os.makedirs(CALIB_DIR, exist_ok=True)

    def mover(self, vx, vy, t):
        try:
            # 1. Asegurar que cualquier movimiento previo se detenga
            self.ptz.Stop({'ProfileToken': self.token})
            
            # 2. Configurar y enviar el movimiento continuo
            req = self.ptz.create_type('ContinuousMove')
            req.ProfileToken = self.token
            req.Velocity = {'PanTilt': {'x': vx, 'y': vy}}
            
            # Intentamos enviar el Timeout por si acaso, pero no confiamos solo en él
            req.Timeout = f"PT{t}S" 
            self.ptz.ContinuousMove(req)
            
            # 3. Dormir el tiempo exacto del movimiento
            time.sleep(t)
            
            # 4. Forzar la parada (Crucial si el Timeout falla)
            self.ptz.Stop({'ProfileToken': self.token})
            
            # 5. Pausa de estabilización física de la cámara
            time.sleep(0.5) 
        except Exception as e: 
            print(f"Error PTZ: {e}")

    def home(self):
        print("\nSincronizando HOME (X e Y)...")
        self.mover(1.0, 0, 11.0) # Sincroniza X #
        self.mover(0, 1.0, 5.0)  # Sincroniza Y (Suelo) #[cite: 1]
        self.curr_pan = 0

    def get_img(self):
        try:
            r = requests.get(HTTP_SNAP_URL, timeout=10)
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            return cv2.rotate(img, cv2.ROTATE_180) #[cite: 1]
        except: return None

    def ejecutar(self):
        self.home()
        cv2.namedWindow("CALIBRACION")
        
        for i in range(1, 4):
            print(f"\nPOSICIÓN {i}: Mueve con 'A'/'D' y pulsa ENTER.")
            while True:
                img = self.get_img()
                if img is None: continue
                cv2.imshow("CALIBRACION", img)
                k = cv2.waitKey(1) & 0xFF
                if k == ord('a') and self.curr_pan > 0:
                    self.mover(1, 0, 0.5); self.curr_pan -= 1 #[cite: 1]
                elif k == ord('d'):
                    self.mover(-1, 0, 0.5); self.curr_pan += 1 #[cite: 1]
                elif k == 13: break 

            # --- DIBUJO DEL ROI (Área de vigilancia) ---
            puntos_roi = [] 
            def mouse_callback(event, x, y, flags, param):
                if event == cv2.EVENT_LBUTTONDOWN:
                    puntos_roi.append((x, y)) #[cite: 1]

            cv2.setMouseCallback("CALIBRACION", mouse_callback) #[cite: 1]
            print(f"   1. Haz clic para dibujar el ROI de la POS {i}. ENTER para confirmar.")
            while True:
                temp_img = self.get_img()
                if len(puntos_roi) > 1:
                    cv2.polylines(temp_img, [np.array(puntos_roi)], True, (255, 0, 0), 2) #[cite: 1]
                cv2.imshow("CALIBRACION", temp_img)
                if cv2.waitKey(1) == 13: break

            # --- SELECCIÓN DE ANCLAJE (Referencia en parasol) ---
            print(f"   2. Selecciona el ANCLAJE (Parasol) para la POS {i}.")
            roi_ref = cv2.selectROI("CALIBRACION", temp_img) #[cite: 1]
            x, y, w, h = roi_ref
            plantilla = temp_img[y:y+h, x:x+w]
            
            self.misiones.append({
                "id": i, "pan_abs": self.curr_pan, "roi": puntos_roi,
                "plantilla": plantilla, "centro": (x + w//2, y + h//2)
            })
            cv2.imwrite(str(CALIB_DIR / f"ref_{i}.png"), plantilla)

        # Guardar configuración #[cite: 1]
        datos_json = [{"id": m["id"], "pan_abs": m["pan_abs"], "roi": m["roi"], "centro": m["centro"]} for m in self.misiones]
        with open(CONFIG_FILE, 'w') as f:
            json.dump(datos_json, f, indent=4)
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ConfiguradorCompleto().ejecutar()