"""Estado de actualización de los datos (SOLO LECTURA) para el indicador «Datos actualizados al» del dashboard.

Fuentes de información, de la más básica a la más completa (todo es opcional salvo mat_resumen_estado):
  * energrid.mat_resumen_estado   ya existe: fecha/hora del último refresco de los resúmenes mat_* (la escribe usp_refrescar_resumenes).
  * energrid.aud_refresco_log     (sql/05_resumenes_sla.sql) resultado de cada ejecución: OK / ERROR / EN_CURSO.
  * energrid.cfg_sla_fuente       (idem) frecuencia, hora límite, tiempo máximo y responsable por fuente. PROPUESTA hasta que
                                  estado_aprobacion = 'APROBADO'; el dashboard nunca la presenta como SLA contractual vigente.
  * energrid.mat_fuente_estado    (idem) dato más reciente de cada fuente en el último refresco OK.
Sin los objetos del script el indicador sigue funcionando con mat_resumen_estado y el umbral de config.REFRESCO_MAX_HORAS_PROPUESTA.
Este módulo no ejecuta DDL, no escribe en SQL Server y no lanza el refresco.
"""
from config import SCHEMA as S, REFRESCO_MAX_HORAS_PROPUESTA

TTL = 60            # segundos de caché de estas consultas (el estado cambia, pero no hace falta más fino)
EN_CURSO_MAX_MIN = 120   # un EN_CURSO más viejo se considera colgado y se ignora

TEXTOS = {
    "ok": "Al día",
    "atrasado": "Actualización atrasada",
    "error": "Error en la última actualización",
    "en_curso": "Actualizando…",
    "sin_datos": "Sin registro de actualización",
}


def decidir_estado(min_desde, umbral_h, ultima):
    """Estado del indicador. `min_desde`: minutos desde el último refresco OK de los resúmenes (None = no hay registro).
    `ultima`: última ejecución registrada (dict con estado, min_desde_inicio, posterior) o None.
    Prioridad: en curso > error posterior al último refresco > atrasado por umbral > al día."""
    if min_desde is None:
        return "sin_datos"
    if ultima:
        if ultima.get("estado") == "EN_CURSO" and (ultima.get("min_desde_inicio") or 0) <= EN_CURSO_MAX_MIN:
            return "en_curso"
        if ultima.get("estado") == "ERROR" and ultima.get("posterior"):
            return "error"
    if umbral_h is not None and min_desde > umbral_h * 60:
        return "atrasado"
    return "ok"


def _estado(q) -> dict:
    have = {r["name"] for r in q(
        f"SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(N'{S}') "
        "AND name IN (N'aud_refresco_log', N'cfg_sla_fuente', N'mat_fuente_estado', N'mat_resumen_estado')", ttl=TTL)}

    ref = q(f"""SELECT TOP 1 CONVERT(varchar(19), refrescado_en, 126) actualizado_al, duracion_seg,
                DATEDIFF(MINUTE, refrescado_en, SYSDATETIME()) min_desde
                FROM [{S}].[mat_resumen_estado] ORDER BY refrescado_en DESC""", ttl=TTL) if "mat_resumen_estado" in have else []
    ref = ref[0] if ref else {}

    ultima = None
    if "aud_refresco_log" in have and "mat_resumen_estado" in have:
        rows = q(f"""SELECT TOP 1 estado, CONVERT(varchar(19), iniciado_en, 126) iniciado_en,
                     CONVERT(varchar(19), finalizado_en, 126) finalizado_en, duracion_seg, origen,
                     DATEDIFF(MINUTE, iniciado_en, SYSDATETIME()) min_desde_inicio,
                     CASE WHEN finalizado_en > ISNULL((SELECT MAX(refrescado_en) FROM [{S}].[mat_resumen_estado]), '19000101')
                          THEN 1 ELSE 0 END posterior
                     FROM [{S}].[aud_refresco_log] WHERE estado IN ('EN_CURSO', 'OK', 'ERROR') ORDER BY run_id DESC""", ttl=TTL)
        ultima = rows[0] if rows else None

    fuentes, umbral_h, umbral_aprobado = [], REFRESCO_MAX_HORAS_PROPUESTA, False
    if "cfg_sla_fuente" in have:
        join = (f"LEFT JOIN [{S}].[mat_fuente_estado] f ON f.fuente = c.fuente", "CONVERT(varchar(10), f.fecha_dato_max, 23)") \
            if "mat_fuente_estado" in have else ("", "CAST(NULL AS varchar(10))")
        cfg = q(f"""SELECT c.fuente, c.descripcion, c.frecuencia, CONVERT(varchar(5), c.hora_limite, 108) hora_limite,
                    c.max_horas_actualizacion max_horas, c.responsable, c.estado_aprobacion, {join[1]} fecha_dato_max
                    FROM [{S}].[cfg_sla_fuente] c {join[0]} ORDER BY c.orden""", ttl=TTL)
        for r in cfg:
            if r["fuente"] == "resumenes":
                if r["max_horas"] is not None:
                    umbral_h = r["max_horas"]
                umbral_aprobado = r["estado_aprobacion"] == "APROBADO"
            else:
                fuentes.append(dict(r, aprobado=r["estado_aprobacion"] == "APROBADO"))

    corte = q(f"SELECT CONVERT(varchar(10), fecha_corte, 23) fecha_corte FROM [{S}].[cfg_parametros] WHERE id = 1", ttl=TTL)

    min_desde = ref.get("min_desde")
    estado = decidir_estado(min_desde, umbral_h, ultima)
    return dict(
        estado=estado, estado_texto=TEXTOS[estado],
        actualizado_al=ref.get("actualizado_al"), duracion_seg=ref.get("duracion_seg"),
        horas_desde_actualizacion=round(min_desde / 60, 1) if min_desde is not None else None,
        umbral_horas=umbral_h, umbral_aprobado=umbral_aprobado,
        ultima_ejecucion=(None if not ultima else {k: ultima.get(k) for k in ("estado", "iniciado_en", "finalizado_en", "duracion_seg", "origen")}),
        registro_ejecuciones="aud_refresco_log" in have,
        sla_configurado="cfg_sla_fuente" in have, fuentes=fuentes,
        sla_aprobado=bool(fuentes) and all(f["aprobado"] for f in fuentes) and umbral_aprobado,
        periodo_hasta=corte[0]["fecha_corte"] if corte else None,
    )


def estado_datos(q) -> dict:
    """Nunca lanza: si algo falla devuelve estado 'sin_datos' para no tumbar el dashboard por un indicador."""
    try:
        return _estado(q)
    except Exception as e:  # noqa: BLE001
        print("[data-status] error:", e)
        return dict(estado="sin_datos", estado_texto=TEXTOS["sin_datos"], actualizado_al=None, fuentes=[], sla_aprobado=False)
