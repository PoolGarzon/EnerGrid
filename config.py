"""Conexión a EnerGrid usando la sesión de Windows (sin contraseña embebida)."""
import urllib.parse
import os
SERVER = r"localhost\SQLEXPRESS"
DATABASE = "EnerGrid"
SCHEMA = "energrid"
DRIVER = "ODBC Driver 17 for SQL Server"

# Actualización de datos. PROPUESTA TÉCNICA pendiente de aprobación (no es un SLA contractual vigente).
# Solo se usa como respaldo si energrid.cfg_sla_fuente aún no existe; si existe, manda la fila 'resumenes' de esa tabla.
# Horas máximas desde el último refresco de los resúmenes mat_* antes de mostrar «Actualización atrasada» (24 h + 2 h de tolerancia).
REFRESCO_MAX_HORAS_PROPUESTA = 26
# Horas locales del equipo. Corte de INGESTA != hora limite de disponibilidad.
CORTE_INGESTA_PROPUESTA = "06:00"
REFRESCO_DIARIO_PROPUESTA = "08:10"
DISPONIBILIDAD_DIARIA_PROPUESTA = "09:00"
# Diseno academico; no existe un consumidor incremental activado por estas constantes.
INTERRUPCIONES_MICROLOTE_MIN_PROPUESTA = 30
INTERRUPCIONES_LATENCIA_MAX_MIN_PROPUESTA = 60
INTERRUPCIONES_INCREMENTAL_IMPLEMENTADO = False

def conn_str() -> str:
    user, password = os.getenv("ENERGRID_SQL_USER"), os.getenv("ENERGRID_SQL_PASSWORD")
    if user and password is None:
        raise ValueError("Falta ENERGRID_SQL_PASSWORD en el entorno del proceso")
    # Escapar valores ODBC: la clave nunca se concatena sin delimitar.
    quote = lambda value: "{" + value.replace("}", "}}") + "}"
    auth = ("UID=" + quote(user) + ";PWD=" + quote(password) + ";") if user else "Trusted_Connection=yes;"
    return (f"DRIVER={{{DRIVER}}};"
            f"SERVER={SERVER};"
            f"DATABASE={DATABASE};"
            f"{auth}"
            "TrustServerCertificate=yes;")

def sqlalchemy_url() -> str:
    return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(conn_str())
