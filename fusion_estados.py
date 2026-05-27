"""
fusion_estados.py — Cerebro del sistema.

Suscribe a todos los topics MQTT de los módulos, toma decisiones
sobre estados (visibilidad, ocupación, conexión) y:
  - Escribe helipuertos.json  (única fuente de verdad para app.py)
  - Publica en heliwarden/log (para que app.py lo sirva al frontend)
  - Envía correos de alerta   (única responsable de alertas por correo)

Corre como proceso independiente: `python fusion_estados.py`
"""

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent))
from alertas_correo import enviar_alerta_conexion, enviar_alerta_vpn

# ── Configuración ─────────────────────────────────────────────────────────────

load_dotenv()
BASE_DIR   = Path(__file__).parent
HELI_FILE  = BASE_DIR / "helipuertos.json"

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

TIMEOUT_ALERTA_CORREO     = int(os.getenv("TIMEOUT_ALERTA_CORREO", 30))
UMBRAL_FALLOS_VISIBILIDAD = 3
UMBRAL_OBSTACULO          = 3

# Mapeo preset → (id_helipuerto, zona)
PRESET_ZONA_MAP = {
    1: ("1", "plataforma"),
    2: ("1", "ruta_1"),
    3: ("1", "ruta_2"),
}

ZONA_LABELS = {
    "plataforma": "Plataforma",
    "ruta_1":     "Ruta 1",
    "ruta_2":     "Ruta 2",
}

# ── Estado interno ────────────────────────────────────────────────────────────

_lock = threading.Lock()

_estado_conexion = {
    "online":          True,
    "t_desconexion":   None,
    "alerta_enviada":  False,
}

_fallos_referencia: dict = {}

# Historial de detecciones por clave "id_heli_zona"
# {clave: {"objetos": [...], "ocupacion_actual": str}}
_historial_detecciones: dict = {}


# ── Helpers JSON ──────────────────────────────────────────────────────────────

_file_lock = threading.Lock()

def _load_heli() -> dict:
    with _file_lock:
        if not HELI_FILE.exists():
            return {"config": {}, "logs": []}
        with open(HELI_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

def _save_heli(data: dict) -> None:
    with _file_lock:
        with open(HELI_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


# ── MQTT cliente ──────────────────────────────────────────────────────────────

_mqtt_client: mqtt.Client = None


def _publicar_log(nivel: str, mensaje: str) -> None:
    payload = json.dumps({"nivel": nivel, "mensaje": mensaje})
    if _mqtt_client:
        _mqtt_client.publish("heliwarden/log", payload, qos=1)

    data = _load_heli()
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data.setdefault("logs", []).append({"timestamp": ts, "nivel": nivel, "mensaje": mensaje})
    if len(data["logs"]) > 1000:
        data["logs"] = data["logs"][-1000:]
    _save_heli(data)


# ── Procesadores por topic ────────────────────────────────────────────────────

def _procesar_conexion(payload: dict) -> None:
    """Topic: heliwarden/conexion"""
    with _lock:
        online         = payload.get("online", True)
        segundos_caida = payload.get("segundos_caida", 0)
        prev_online    = _estado_conexion["online"]

        if not online and prev_online:
            _estado_conexion["online"]         = False
            _estado_conexion["t_desconexion"]  = time.time()
            _estado_conexion["alerta_enviada"] = False
            _publicar_log("ALARM", "Conexión perdida con la cámara")

        elif not online and not prev_online:
            t_caida = _estado_conexion.get("t_desconexion") or time.time()
            elapsed = int(time.time() - t_caida)
            if not _estado_conexion["alerta_enviada"] and elapsed >= TIMEOUT_ALERTA_CORREO:
                threading.Thread(
                    target=enviar_alerta_conexion, args=(elapsed,), daemon=True
                ).start()
                _estado_conexion["alerta_enviada"] = True
                _publicar_log("ALARM", f"CÁMARA DESCONECTADA: sin señal durante {elapsed}s")

        elif online and not prev_online:
            _estado_conexion["online"]         = True
            _estado_conexion["t_desconexion"]  = None
            _estado_conexion["alerta_enviada"] = False
            _publicar_log("INFO", "Conexión con la cámara recuperada")


def _procesar_patrulla(payload: dict) -> None:
    """Topic: heliwarden/patrulla"""
    with _lock:
        id_preset  = payload.get("preset")
        ref_ok     = payload.get("referencia_ok", True)
        fallos     = payload.get("fallos", 0)
        razon      = payload.get("razon", "")

        if id_preset not in PRESET_ZONA_MAP:
            return

        id_heli, zona = PRESET_ZONA_MAP[id_preset]
        zona_label    = ZONA_LABELS.get(zona, zona)

        _fallos_referencia[id_preset] = fallos

        if not ref_ok:
            _publicar_log("ALARM", f"FALLO P{id_preset} ({razon}) — intento {fallos}")
            if fallos >= UMBRAL_FALLOS_VISIBILIDAD:
                _cambiar_visibilidad(id_heli, zona, zona_label, "Mala")
        else:
            _cambiar_visibilidad(id_heli, zona, zona_label, "Buena")
            _fallos_referencia[id_preset] = 0
            _publicar_log("INFO", f"Fijado preset {id_preset} — {zona_label} (H-{id_heli})")


def _procesar_deteccion(payload: dict) -> None:
    """
    Topic: heliwarden/deteccion

    Payload enriquecido (nuevo formato):
      {
        "preset":           int,
        "objetos":          [{"clase": str, "cx": float, "cy": float, "confianza": float}],
        "occupancy_status": "LIBRE" | "OCUPADO_OBJETO" | "OCUPADO_HELICOPTERO",
        "camera_ok":        bool,
        "blur_score":       float,
      }

    Si el módulo de detección es antiguo y no envía occupancy_status,
    se calcula aquí mediante el historial de repeticiones (comportamiento anterior).
    """
    with _lock:
        id_preset        = payload.get("preset")
        objetos          = payload.get("objetos", [])
        occupancy_status = payload.get("occupancy_status")   # puede ser None si es payload antiguo
        camera_ok        = payload.get("camera_ok", True)
        blur_score       = payload.get("blur_score", 0.0)

        if id_preset not in PRESET_ZONA_MAP:
            return

        id_heli, zona = PRESET_ZONA_MAP[id_preset]
        zona_label    = ZONA_LABELS.get(zona, zona)
        clave         = f"{id_heli}_{zona}"

        # ── Log de calidad de imagen ──────────────────────────────────────────
        if not camera_ok:
            _publicar_log("ALARM",
                          f"Imagen con baja nitidez en {zona_label} (H-{id_heli}) "
                          f"— blur={blur_score:.0f}. Detección poco fiable.")

        # ── Historial de repeticiones ─────────────────────────────────────────
        # Se conserva para el cálculo de UMBRAL_OBSTACULO cuando occupancy_status
        # no viene del módulo (compatibilidad hacia atrás).
        entrada = _historial_detecciones.setdefault(clave, {
            "objetos":          [],
            "ocupacion_actual": "Despejado",
        })

        RANGO_COORDS = 30

        if not objetos:
            entrada["objetos"].clear()
        else:
            vistos_ids = set()
            for det in objetos:
                encontrado = False
                for i, obj in enumerate(entrada["objetos"]):
                    if (obj["clase"] == det["clase"]
                            and abs(obj["cx"] - det["cx"]) <= RANGO_COORDS
                            and abs(obj["cy"] - det["cy"]) <= RANGO_COORDS):
                        obj["repeticiones"] += 1
                        obj["cx"] = det["cx"]
                        obj["cy"] = det["cy"]
                        vistos_ids.add(i)
                        encontrado = True
                        break
                if not encontrado:
                    entrada["objetos"].append({
                        "clase":        det["clase"],
                        "cx":           det["cx"],
                        "cy":           det["cy"],
                        "repeticiones": 1,
                    })
                    vistos_ids.add(len(entrada["objetos"]) - 1)
            entrada["objetos"] = [
                o for i, o in enumerate(entrada["objetos"]) if i in vistos_ids
            ]

        # ── Determinar ocupación final ────────────────────────────────────────
        if occupancy_status is not None:
            # Módulo nuevo: usar el estado semántico que ya viene calculado.
            # Traducimos a los valores que espera el frontend/helipuertos.json.
            if occupancy_status == "OCUPADO_HELICOPTERO":
                nueva_ocup = "Obstáculo"      # helicóptero en zona → obstáculo operativo
                tipo_log   = "Helicóptero"
            elif occupancy_status == "OCUPADO_OBJETO":
                nueva_ocup = "Obstáculo"
                tipo_log   = "Objeto detectado"
            else:  # LIBRE
                nueva_ocup = "Despejado"
                tipo_log   = ""
        else:
            # Fallback: comportamiento original basado en repeticiones
            hay_obstaculo = any(
                o["repeticiones"] >= UMBRAL_OBSTACULO for o in entrada["objetos"]
            )
            nueva_ocup = "Obstáculo" if hay_obstaculo else "Despejado"
            tipo_log   = "Objeto detectado"

        ocup_anterior = entrada["ocupacion_actual"]

        # Detecciones confirmadas para guardar en JSON
        dets_limpias = [
            {
                "clase":       o["clase"],
                "cx":          o["cx"],
                "cy":          o["cy"],
                "repeticiones": o["repeticiones"],
            }
            for o in entrada["objetos"]
            if o["repeticiones"] >= UMBRAL_OBSTACULO
        ] if occupancy_status is None else [
            {"clase": o["clase"], "cx": o["cx"], "cy": o["cy"]}
            for o in objetos
        ]

        _actualizar_ocupacion_heli(id_heli, zona, nueva_ocup, occupancy_status or "DESCONOCIDO", dets_limpias)

        # ── Log solo si cambia el estado ──────────────────────────────────────
        if nueva_ocup != ocup_anterior:
            if nueva_ocup == "Obstáculo":
                if occupancy_status == "OCUPADO_HELICOPTERO":
                    _publicar_log("ALARM",
                                  f"¡ALERTA! Helicóptero detectado en {zona_label} (H-{id_heli})")
                else:
                    tipos = list({o["clase"] for o in objetos})
                    _publicar_log("ALARM",
                                  f"¡ALERTA! Obstáculo en {zona_label} (H-{id_heli}): "
                                  f"{', '.join(tipos)}")
            else:
                _publicar_log("INFO", f"Zona despejada: {zona_label} (H-{id_heli})")

        entrada["ocupacion_actual"] = nueva_ocup


# ── Escritores en helipuertos.json ────────────────────────────────────────────

def _cambiar_visibilidad(id_heli: str, zona: str, zona_label: str, nueva_vis: str) -> None:
    data   = _load_heli()
    config = data.setdefault("config", {})
    heli   = config.setdefault(id_heli, {})

    seccion = (
        heli.setdefault("plataforma", {})
        if zona == "plataforma"
        else heli.setdefault("rutas", {}).setdefault(zona, {})
    )

    if seccion.get("visibilidad") == nueva_vis:
        return

    seccion["visibilidad"] = nueva_vis
    seccion["rango"]       = 4 if nueva_vis == "Mala" else 2
    _save_heli(data)

    if nueva_vis == "Mala":
        _publicar_log("ALARM", f"Visibilidad MALA en {zona_label} (H-{id_heli}): sin referencia")
    else:
        _publicar_log("INFO", f"Visibilidad recuperada en {zona_label} (H-{id_heli})")


def _actualizar_ocupacion_heli(
    id_heli: str,
    zona: str,
    nueva_ocup: str,
    occupancy_status: str,
    dets: list,
) -> None:
    """
    Escribe ocupación, estado semántico y detecciones en helipuertos.json.

    Campos escritos:
      - ocupacion:         "Despejado" | "Obstáculo"   (para colores y lógica)
      - occupancy_status:  "LIBRE" | "OCUPADO_OBJETO" | "OCUPADO_HELICOPTERO"  (para el frontend)
      - rango:             1–4
      - detecciones:       lista de objetos
    """
    data   = _load_heli()
    config = data.setdefault("config", {})
    heli   = config.setdefault(id_heli, {})
    rango  = 4 if nueva_ocup == "Obstáculo" else 2

    seccion = (
        heli.setdefault("plataforma", {})
        if zona == "plataforma"
        else heli.setdefault("rutas", {}).setdefault(zona, {})
    )

    seccion.update({
        "ocupacion":        nueva_ocup,
        "occupancy_status": occupancy_status,
        "rango":            rango,
        "detecciones":      dets,
    })
    _save_heli(data)


def _procesar_reset() -> None:
    """Topic: heliwarden/patrulla/reset"""
    with _lock:
        _fallos_referencia.clear()

        data = _load_heli()
        for heli in data.get("config", {}).values():
            secciones = [heli.get("plataforma", {})] + list(heli.get("rutas", {}).values())
            for seccion in secciones:
                seccion["ocupacion"]        = "No analizado"
                seccion["occupancy_status"] = "NO_ANALIZADO"
                seccion["visibilidad"]      = "No analizado"
                seccion["rango"]            = 1
                seccion["detecciones"]      = []
        _save_heli(data)

        _publicar_log("INFO", "Patrulla iniciada — estados reseteados a 'No analizado'")
        print("[fusion] Reset completado.")


# ── Callback MQTT ─────────────────────────────────────────────────────────────

def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic   = msg.topic

        if topic == "heliwarden/conexion":
            _procesar_conexion(payload)
        elif topic == "heliwarden/patrulla":
            _procesar_patrulla(payload)
        elif topic == "heliwarden/patrulla/reset":
            _procesar_reset()
        elif topic == "heliwarden/deteccion":
            _procesar_deteccion(payload)
    except Exception as e:
        print(f"[fusion] Error procesando {msg.topic}: {e}")


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ fusion_estados conectado al broker MQTT.")
        client.subscribe([
            ("heliwarden/conexion",       1),
            ("heliwarden/patrulla",       1),
            ("heliwarden/patrulla/reset", 1),
            ("heliwarden/deteccion",      1),
        ])
    else:
        print(f"❌ fusion_estados: fallo de conexión MQTT (rc={rc})")


def _on_disconnect(client, userdata, rc):
    print(f"⚠️  fusion_estados desconectado del broker MQTT (rc={rc}). Reintentando...")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _mqtt_client = mqtt.Client(client_id="heliwarden-fusion")
    _mqtt_client.on_connect    = _on_connect
    _mqtt_client.on_message    = _on_message
    _mqtt_client.on_disconnect = _on_disconnect
    _mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)

    print(f"Conectando a broker MQTT en {MQTT_BROKER}:{MQTT_PORT}...")
    _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    _mqtt_client.loop_forever()