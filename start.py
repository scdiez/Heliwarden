"""
start.py — Arranca el sistema Heliwarden completo.

Orden de arranque:
  1. Broker MQTT (comprueba el configurado en .env; arranca Mosquitto local solo si es localhost)
  2. fusion_estados.py
  3. Modulos/modulo_conexion.py
  4. Modulos/modulo_patrulla.py
  5. Modulos/modulo_deteccion.py
  6. app.py (Flask)

Uso:
  python start.py             → arranca todo
  python start.py --sin-yolo  → arranca todo excepto modulo_deteccion
  Ctrl+C                      → para todos los procesos
"""

import argparse
import subprocess
import sys
import time
import os
import signal
import socket
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
PYTHON   = sys.executable

load_dotenv(ROOT_DIR / ".env")

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

ESPERA_ENTRE_PROCESOS = 2

procesos: list[subprocess.Popen] = []


def _arrancar(nombre: str, script: Path, extra_args: list = None) -> subprocess.Popen:
    args = [PYTHON, str(script)] + (extra_args or [])
    print(f"  ▶ Arrancando {nombre}...")
    p = subprocess.Popen(args, cwd=str(ROOT_DIR))
    procesos.append(p)
    time.sleep(ESPERA_ENTRE_PROCESOS)
    return p


def _broker_responde(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _arrancar_mosquitto() -> subprocess.Popen | None:
    """Solo intenta arrancar Mosquitto si el broker configurado es localhost."""
    if MQTT_BROKER not in ("localhost", "127.0.0.1"):
        # Broker remoto — no intentamos arrancar nada local
        if _broker_responde(MQTT_BROKER, MQTT_PORT):
            print(f"  ✅ Broker MQTT remoto disponible en {MQTT_BROKER}:{MQTT_PORT}")
        else:
            print(f"  ❌ No se puede conectar al broker MQTT en {MQTT_BROKER}:{MQTT_PORT}")
            print(f"     Comprueba que el broker está activo y accesible.")
        return None

    # Broker local — comportamiento original
    if _broker_responde("localhost", MQTT_PORT):
        print(f"  ✅ Mosquitto ya está corriendo en localhost:{MQTT_PORT}")
        return None

    print("  ▶ Intentando arrancar Mosquitto...")
    try:
        if sys.platform == "win32":
            p = subprocess.Popen(
                ["mosquitto", "-v"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            p = subprocess.Popen(["mosquitto", "-v"])
        procesos.append(p)
        time.sleep(2)
        if _broker_responde("localhost", MQTT_PORT):
            print("  ✅ Mosquitto arrancado.")
        else:
            print("  ⚠️  Mosquitto no respondió. ¿Está instalado?")
        return p
    except FileNotFoundError:
        print(
            "\n  ❌ No se encontró 'mosquitto' en el PATH.\n"
            "     Instálalo siguiendo las instrucciones en README_MQTT.md\n"
            "     o arráncalo manualmente antes de ejecutar start.py\n"
        )
        return None


def _parar_todo(signum=None, frame=None):
    print("\n\nParando todos los procesos...")
    for p in reversed(procesos):
        try:
            p.terminate()
        except Exception:
            pass
    for p in reversed(procesos):
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    print("Sistema detenido.")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Arrancador de Heliwarden")
    parser.add_argument("--sin-yolo", action="store_true",
                        help="No arrancar modulo_deteccion")
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _parar_todo)
    signal.signal(signal.SIGTERM, _parar_todo)

    print("=" * 55)
    print("  HELIWARDEN — Arranque del sistema")
    print("=" * 55)

    _arrancar_mosquitto()

    if not _broker_responde(MQTT_BROKER, MQTT_PORT):
        print(f"\n❌ No se puede continuar sin el broker MQTT en {MQTT_BROKER}:{MQTT_PORT}.")
        sys.exit(1)

    _arrancar("fusion_estados",   ROOT_DIR / "fusion_estados.py")
    _arrancar("modulo_conexion",  ROOT_DIR / "Modulos" / "modulo_conexion.py")
    _arrancar("modulo_patrulla",  ROOT_DIR / "Modulos" / "modulo_patrulla.py")

    if not args.sin_yolo:
        _arrancar("modulo_deteccion", ROOT_DIR / "Modulos" / "modulo_deteccion.py")
    else:
        print("  ⏭  modulo_deteccion omitido (--sin-yolo)")

    _arrancar("app (Flask)", ROOT_DIR / "app.py")

    print("\n✅ Sistema completo arrancado.")
    print(f"   Broker MQTT → {MQTT_BROKER}:{MQTT_PORT}")
    print("   Dashboard → http://localhost:5000")
    print("   Ctrl+C para parar todo.\n")

    while True:
        time.sleep(5)
        caidos = [p for p in procesos if p.poll() is not None]
        for p in caidos:
            print(f"⚠️  Un proceso terminó inesperadamente (PID {p.pid}). Revisa los logs.")


if __name__ == "__main__":
    main()