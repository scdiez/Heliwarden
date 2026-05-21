"""
start.py — Arranca el sistema Heliwarden completo.

Orden de arranque:
  1. Broker Mosquitto (si no está ya corriendo)
  2. fusion_estados.py
  3. modulos/modulo_conexion.py
  4. modulos/modulo_patrulla.py
  5. modulos/modulo_deteccion.py
  6. app.py (Flask)

Uso:
  python start.py             → arranca todo
  python start.py --sin-yolo  → arranca todo excepto modulo_deteccion
  Ctrl+C                      → para todos los procesos

Requisito previo:
  Mosquitto instalado. Ver instrucciones en README_MQTT.md
"""

import argparse
import subprocess
import sys
import time
import os
import signal
from pathlib import Path

BASE_DIR = Path(__file__).parent
PYTHON   = sys.executable   # mismo intérprete que lanzó este script

# Tiempo de espera entre arranques para que cada proceso conecte al broker
ESPERA_ENTRE_PROCESOS = 2  # segundos

procesos: list[subprocess.Popen] = []


def _arrancar(nombre: str, script: Path, extra_args: list = None) -> subprocess.Popen:
    args = [PYTHON, str(script)] + (extra_args or [])
    print(f"  ▶ Arrancando {nombre}...")
    p = subprocess.Popen(args, cwd=str(BASE_DIR))
    procesos.append(p)
    time.sleep(ESPERA_ENTRE_PROCESOS)
    return p


def _esta_mosquitto_corriendo() -> bool:
    """Comprueba si el broker Mosquitto está escuchando en el puerto 1883."""
    import socket
    try:
        with socket.create_connection(("localhost", 1883), timeout=1):
            return True
    except OSError:
        return False


def _arrancar_mosquitto() -> subprocess.Popen | None:
    if _esta_mosquitto_corriendo():
        print("  ✅ Mosquitto ya está corriendo en localhost:1883")
        return None

    print("  ▶ Intentando arrancar Mosquitto...")
    try:
        if sys.platform == "win32":
            # Asume que mosquitto.exe está en el PATH o en C:\Program Files\mosquitto\
            p = subprocess.Popen(
                ["mosquitto", "-v"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            p = subprocess.Popen(["mosquitto", "-v"])
        procesos.append(p)
        time.sleep(2)
        if _esta_mosquitto_corriendo():
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
                        help="No arrancar modulo_deteccion (útil si YOLO no está configurado)")
    args = parser.parse_args()

    # Capturar Ctrl+C y SIGTERM
    signal.signal(signal.SIGINT,  _parar_todo)
    signal.signal(signal.SIGTERM, _parar_todo)

    print("=" * 55)
    print("  HELIWARDEN — Arranque del sistema")
    print("=" * 55)

    # 1. Broker
    _arrancar_mosquitto()

    if not _esta_mosquitto_corriendo():
        print("\n❌ No se puede continuar sin el broker MQTT.")
        print("   Arranca Mosquitto manualmente y vuelve a ejecutar start.py")
        sys.exit(1)

    # 2. Fusion (debe arrancar antes que los módulos para no perder mensajes)
    _arrancar("fusion_estados", BASE_DIR / "fusion_estados.py")

    # 3. Módulos secundarios
    _arrancar("modulo_conexion", BASE_DIR / "modulos" / "modulo_conexion.py")
    _arrancar("modulo_patrulla", BASE_DIR / "modulos" / "modulo_patrulla.py")

    if not args.sin_yolo:
        _arrancar("modulo_deteccion", BASE_DIR / "modulos" / "modulo_deteccion.py")
    else:
        print("  ⏭  modulo_deteccion omitido (--sin-yolo)")

    # 4. Flask
    _arrancar("app (Flask)", BASE_DIR / "app.py")

    print("\n✅ Sistema completo arrancado.")
    print("   Dashboard → http://localhost:5000")
    print("   Ctrl+C para parar todo.\n")

    # Mantener vivo y vigilar procesos
    while True:
        time.sleep(5)
        caidos = [p for p in procesos if p.poll() is not None]
        for p in caidos:
            print(f"⚠️  Un proceso terminó inesperadamente (PID {p.pid}). Revisa los logs.")


if __name__ == "__main__":
    main()
