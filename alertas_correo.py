import smtplib
import os
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

# ── Paths basados en la ubicación de este archivo ─────────────────────────────
MODULOS_DIR = Path(__file__).resolve().parent
ROOT_DIR    = MODULOS_DIR.parent

load_dotenv(ROOT_DIR / ".env")

def _enviar_correo(asunto, cuerpo):
    remitente    = os.getenv("EMAIL_USER")
    password     = os.getenv("EMAIL_PASS")
    destinatario = os.getenv("EMAIL_DEST")
    if not all([remitente, password, destinatario]):
        print("❌ Error: No se encontraron las credenciales en el archivo .env")
        return False
    msg = MIMEText(cuerpo)
    msg['Subject'] = asunto
    msg['From']    = remitente
    msg['To']      = destinatario
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(remitente, password)
            server.send_message(msg)
        print(f"📧 Correo enviado: {asunto}")
        return True
    except Exception as e:
        print(f"Error al enviar correo: {e}")
        return False


def enviar_alerta_vpn(segundos):
    hora = datetime.now().strftime("%H:%M:%S")
    return _enviar_correo(
        f"ALERTA: VPN desconectada ({hora})",
        f"ATENCIÓN: Se ha perdido la conexión VPN.\n"
        f"El gateway de la red no responde desde hace más de {segundos} segundos.\n"
        f"Hora de detección: {hora}.\n"
        f"Por favor, reconecta la VPN.",
    )


def enviar_alerta_conexion(segundos):
    hora = datetime.now().strftime("%H:%M:%S")
    return _enviar_correo(
        f"ALERTA: Desconexión Cámara ({hora})",
        f"ATENCIÓN: Se ha perdido la conexión con la cámara durante más de {segundos} segundos.\n"
        f"Hora de detección: {hora}.\n"
        f"Por favor, revisa el host.",
    )
