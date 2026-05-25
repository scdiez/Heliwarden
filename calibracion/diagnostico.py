"""
calibrar_home.py — Calibra los tiempos de barrido completo de la Foscam SD4H.

Ejecutar: python calibrar_home.py
El script mueve la cámara al extremo izquierdo y luego al extremo superior,
midiendo cuánto tiempo tarda en llegar a los topes mecánicos.
Al final imprime los valores para pegar en modulo_patrulla.py.
"""

import os, time, requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

USER      = os.getenv("CAMERA_USER")
PASS      = os.getenv("CAMERA_PASS")
IP        = os.getenv("CAMERA_IP")
HTTP_PORT = int(os.getenv("CAMERA_HTTP_PORT", 88))

def cgi(cmd, extra=None):
    params = {"cmd": cmd, "usr": USER, "pwd": PASS}
    if extra:
        params.update(extra)
    return requests.get(
        f"http://{IP}:{HTTP_PORT}/cgi-bin/CGIProxy.fcgi",
        params=params, timeout=5
    )

print("\n" + "="*55)
print("  Calibración HOME — Foscam SD4H")
print("="*55)
print("\nEste script mueve la cámara hasta los topes mecánicos.")
print("Observa cuándo deja de moverse (ha llegado al tope).")

# ── Paso 1: barrido horizontal completo hacia la izquierda ────────────────────
input("\n▶ Pulsa ENTER para mover a la IZQUIERDA a velocidad 9...")
print("  Moviendo... (observa cuándo para de moverse)")

t0 = time.time()
cgi("ptzMoveRight", {"speed": 20})

input("  Pulsa ENTER cuando la cámara haya llegado al tope izquierdo...")
t_izq = time.time() - t0
cgi("ptzStopRun")
print(f"  → Tiempo hasta tope izquierdo: {t_izq:.1f}s")

time.sleep(0.5)

# ── Paso 2: barrido vertical completo hacia arriba ────────────────────────────
input("\n▶ Pulsa ENTER para mover hacia ARRIBA a velocidad 9...")
print("  Moviendo... (observa cuándo para de moverse)")

t0 = time.time()
cgi("ptzMoveUp", {"speed": 9})

input("  Pulsa ENTER cuando la cámara haya llegado al tope superior...")
t_arr = time.time() - t0
cgi("ptzStopRun")
print(f"  → Tiempo hasta tope superior: {t_arr:.1f}s")

time.sleep(0.5)

# ── Margen de seguridad (+20%) ────────────────────────────────────────────────
t_izq_safe = round(t_izq * 1.2, 1)
t_arr_safe = round(t_arr * 1.2, 1)

print("\n" + "="*55)
print("  RESULTADO — pega estos valores en modulo_patrulla.py")
print("="*55)
print(f"\n  En _ptz_home():")
print(f'    _ptz_move("left", {t_izq_safe}, speed=9)  # tope izq medido: {t_izq:.1f}s')
print(f'    _ptz_move("up",   {t_arr_safe}, speed=9)  # tope arr medido: {t_arr:.1f}s')
print()
print("  O añade estas líneas a tu .env:")
print(f"    PTZ_HOME_LEFT={t_izq_safe}")
print(f"    PTZ_HOME_UP={t_arr_safe}")
print("="*55 + "\n")
