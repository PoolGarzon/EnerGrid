"""EnerGrid BI Web - API (FastAPI) + frontend estático (ECharts). Reutiliza las vistas SQL existentes.

Rendimiento (las vistas pesadas se recalculaban en cada consulta):
  * Si existen las tablas de resumen energrid.mat_* (ver sql/04_procedimientos.sql) las consultas del dashboard
    las usan en lugar de las vistas (de segundos a milisegundos). Si no existen, se usan las vistas como antes
    (mismos resultados, solo más lento): el script SQL se puede aplicar o revertir sin tocar este archivo.
  * Las consultas del dashboard se ejecutan EN PARALELO.
  * "Último corte de cartera" se lee de fact_cartera_snapshot (índice PK por snapshot_date, ~0 ms).
  * El Top 10 de transformadores (todo el período) se calcula en segundo plano y se guarda en disco (.cache/).
  * Si el proceso de refresco SQL corrió (energrid.mat_resumen_estado), las cachés se invalidan solas.

Filtros (GET /api/dashboard?zones=1,2&month=AAAA-MM&region=Norte&circuit=12&tariff=1&client_type=Residencial):
  * month vacío = "Todo el período" (acumulado de todo lo que hay en las tablas intermedias, sin límite de año).
  * month = un solo mes: KPI, rankings, cartera, alertas, causas, circuitos, controles y Top 10 usan solo ese mes.
    Las series mensuales (tendencias) se devuelven completas y el frontend resalta el mes elegido.
  * zones = filtro de zonas; region = dim_zona.region. Ambos se COMBINAN (intersección: zonas elegidas dentro de la región).
    Los rankings por zona (criticidad, pérdida, morosidad) se devuelven con TODAS las zonas (la normalización min-max
    de la criticidad las necesita) y el frontend filtra: así la misma consulta en caché sirve para cualquier combinación.
  * circuit / tariff / client_type: se aplican solo donde el dato lo permite (ver `na` en la respuesta):
        pérdidas de energía ........ región, zona, circuito (NO tarifa/tipo: la energía inyectada no se mide por tarifa; sin prorrateos)
        AMI, consumo estimado ...... región, zona, circuito, tarifa, tipo
        cobranza (CE) .............. región, zona, circuito, tarifa, tipo
        cartera, DSO, aging, >60 d . región, zona, circuito, tarifa, tipo
        SAIDI/SAIFI/MTTR/causas .... región, zona, circuito (NO tarifa/tipo)
    Circuito, tarifa y tipo necesitan las tablas de sql/04_procedimientos.sql; sin ellas la API responde 409 con un
    mensaje claro (los filtros de región/zona/mes siguen funcionando con las mat_* de siempre).
  * Disponibilidad AMI (misma fórmula en KPI, serie `ami`, `ami_zone` del mapa y alertas de vw_alertas):
        Σ lecturas_validas / Σ lecturas_esperadas de medidores AMI (válida = estado 'Leído' con ami_valid = 1; esperada = una lectura
        programada por medidor AMI y mes, incluye Leído/Estimado/Faltante). Con una lectura por medidor-mes equivale a la definición del caso
        (medidores con lectura válida / medidores AMI activos del mes); evidencia y decisión en README.md y sql/06_verificaciones.sql.
  * Consumo estimado (kpi.estimadas, serie `est`): Σ facturas_estimadas / Σ facturas_emitidas (is_estimated = 1: lectura Estimado o Faltante), es decir
        un PORCENTAJE de facturas, no kWh. kpi.est_n / kpi.emit_n traen las cantidades. Referencia: targets.est_meta = 1 - cfg_parametros.alerta_ami_min.
        mat_consumo_estimado_mes coincide con fact_facturacion (emitidas y estimadas) en los 12 meses y con filtros de zona/tarifa/tipo.
  * Todos los valores del usuario viajan como parámetros (?) de pyodbc y los identificadores/valores se validan contra
    el catálogo (dim_zona, dim_circuito, dim_tarifa, dim_cliente); nada del usuario se concatena en el SQL.
"""
import datetime as dt, decimal, json, math, threading, time, re, contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pyodbc
from fastapi import FastAPI, Query, Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import SCHEMA as S, conn_str
from estado_datos import estado_datos, TEXTOS

BASE = Path(__file__).parent
_cache: dict = {}
TTL = 120            # caché en memoria de consultas (segundos)
TOP10_TTL = 6 * 3600  # vigencia del Top 10 anual (se refresca en segundo plano)
TOP10_FILE = BASE / ".cache" / "top10.json"
CATALOG_TTL = 60
_query_locks = {}
_query_locks_guard = threading.Lock()
_metrics = contextvars.ContextVar("sql_metrics", default=None)
MAX_CACHE = 600      # tope de entradas (cada combinación de filtros genera consultas distintas)

# Metas de referencia mostradas en los dashboards (ajustar si el enunciado del caso define otras).
# Los umbrales de AMI, z-score y deterioro SAIDI NO van aquí: se leen de energrid.cfg_parametros.
TARGETS = {"perdida_meta": 0.094, "ce_meta": 0.95}

# Tablas creadas por sql/04_procedimientos.sql (circuito / tarifa / tipo de cliente).
DIM_TABLES = frozenset({"mat_perdida_circuito_mes", "mat_cartera_dim", "mat_facturado_dim",
                        "mat_cobranza_dim_mes", "mat_ami_dim_mes"})


def _clean(v):
    if isinstance(v, decimal.Decimal): v = float(v)
    if isinstance(v, (dt.date, dt.datetime)): return v.isoformat()[:10]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
    if isinstance(v, bool): return v
    return v


# ---------------------------------------------------------------- Consultas parametrizadas
# El SQL usa marcadores con nombre (:z lista, :c circuito, :t tarifa, :k tipo, :a/:b límites de mes, :y/:m año/mes).
# bind() los convierte a "?" de pyodbc en el orden en que aparecen: los valores NUNCA se concatenan en el texto SQL.
_TOK = re.compile(r":([A-Za-z_]\w*)")


def bind(sql: str, vals: Optional[dict]):
    params: list = []

    def rep(m):
        k = m.group(1)
        if not vals or k not in vals:
            return m.group(0)
        v = vals[k]
        if isinstance(v, (list, tuple)):
            params.extend(v)
            return ",".join(["?"] * len(v))
        params.append(v)
        return "?"
    return _TOK.sub(rep, sql), params


def _exec(sql: str, params=()) -> list[dict]:
    """Ejecuta un lote SQL (puede incluir #temporales) y devuelve el primer conjunto de resultados."""
    started = time.perf_counter()
    con = pyodbc.connect(conn_str(), timeout=15)
    try:
        cur = con.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [{c: _clean(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
    finally:
        con.close()
        metrics = _metrics.get()
        if metrics is not None:
            metrics.append((time.perf_counter() - started) * 1000)


def q(sql: str, vals: Optional[dict] = None, ttl: int = TTL) -> list[dict]:
    text, params = bind(sql, vals)
    key = (text, tuple(params))
    generation = _mat["stamp"]
    # Un calculo por clave; retirar el lock solo cuando no quedan lectores esperando.
    with _query_locks_guard:
        entry = _query_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            hit = _cache.get(key)
            if ttl and hit and time.time() - hit[0] < ttl:
                return hit[1]
            rows = _exec(text, params)
            if len(_cache) >= MAX_CACHE:
                _cache.clear()
            if ttl and generation == _mat["stamp"]:
                _cache[key] = (time.time(), rows)
            return rows
    finally:
        with _query_locks_guard:
            entry[1] -= 1
            if not entry[1]:
                _query_locks.pop(key, None)



def run_parallel(queries: dict, vals: Optional[dict] = None) -> dict:
    """Ejecuta varias consultas a la vez (una conexión por hilo)."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(contextvars.copy_context().run, q, sql, vals) for k, sql in queries.items()}
        return {k: f.result() for k, f in futs.items()}


# ---------------------------------------------------------------- Catálogo y validación de filtros
_catalog = {"ts": 0.0, "v": None}


def catalog() -> dict:
    """Listas válidas (zonas, regiones, circuitos, tarifas, tipos de cliente) leídas de las dimensiones; caché de 60 s e invalidacion por publicacion."""
    if _catalog["v"] is not None and time.time() - _catalog["ts"] < CATALOG_TTL:
        return _catalog["v"]
    zones = q(f"SELECT zone_id, zone_name, region FROM [{S}].[dim_zona] WHERE zone_id > 0 ORDER BY zone_name", ttl=CATALOG_TTL)
    circuits = q(f"SELECT circuit_id, circuit_name, zone_id FROM [{S}].[dim_circuito] "
                 f"WHERE circuit_id > 0 AND zone_id > 0 ORDER BY zone_id, circuit_name", ttl=CATALOG_TTL)
    tariffs = q(f"SELECT tariff_id, tariff_name FROM [{S}].[dim_tarifa] ORDER BY tariff_id", ttl=CATALOG_TTL)
    types = q(f"SELECT DISTINCT client_type FROM [{S}].[dim_cliente] WHERE client_type IS NOT NULL ORDER BY client_type", ttl=CATALOG_TTL)
    v = dict(
        zone_region={z["zone_id"]: z["region"] for z in zones},
        regions=sorted({z["region"] for z in zones if z["region"]}),
        circuits=circuits, circuit_zone={c["circuit_id"]: c["zone_id"] for c in circuits},
        tariffs=tariffs, tariff_ids={t["tariff_id"] for t in tariffs},
        types=[t["client_type"] for t in types],
    )
    _catalog.update(ts=time.time(), v=v)
    return v


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Flt:
    ids: tuple                  # zonas efectivas (zonas elegidas ∩ región); vacío = sin filtro de zona
    circuit: Optional[int]
    tariff: Optional[int]
    ctype: Optional[str]
    region: Optional[str]


def resolve(zones: Optional[str], region: Optional[str], circuit: Optional[int],
            tariff: Optional[int], client_type: Optional[str]) -> Flt:
    """Valida y normaliza los filtros; lanza FilterError (-> HTTP 422) ante cualquier valor inválido."""
    mats()
    c = catalog()
    zmap = c["zone_region"]
    ids: list[int] = []
    if zones:
        toks = [t.strip() for t in zones.split(",") if t.strip()]
        if len(toks) > 100 or not all(re.fullmatch(r"-?\d{1,6}", t) for t in toks):
            raise FilterError("El parámetro zones debe ser una lista de identificadores numéricos separados por comas")
        ids = sorted({int(t) for t in toks})
        bad = [z for z in ids if z not in zmap]
        if bad:
            raise FilterError(f"Zona(s) inexistente(s): {', '.join(map(str, bad))}")
    if region:
        if region not in c["regions"]:
            raise FilterError("Región inexistente")
        rz = sorted(z for z, r in zmap.items() if r == region)
        if ids:
            ids = [z for z in ids if z in rz]   # intersección zonas elegidas × región
            if not ids:
                raise FilterError("Las zonas elegidas no pertenecen a la región seleccionada")
        else:
            ids = rz
    if ids and len(ids) == len(zmap):
        ids = []                                  # todas las zonas = sin filtro (misma clave de caché)
    if circuit is not None:
        zc = c["circuit_zone"].get(circuit)
        if zc is None:
            raise FilterError("Circuito inexistente")
        if ids and zc not in ids:
            raise FilterError("El circuito no pertenece a las zonas/región seleccionadas")
    if tariff is not None and tariff not in c["tariff_ids"]:
        raise FilterError("Tarifa inexistente")
    if client_type and client_type not in c["types"]:
        raise FilterError("Tipo de cliente inexistente")
    return Flt(tuple(ids), circuit, tariff, client_type or None, region or None)


# ---------------------------------------------------------------- Tablas de resumen (mat_*) con respaldo en vistas
_mat = {"checked": 0.0, "have": frozenset(), "stamp": None}
_mjobs: dict = {}          # 'AAAA-MM' -> trabajo pesado por mes (ranking de transformadores + control de zonas)
_mjobs_lock = threading.Lock()


def mats() -> frozenset:
    """Tablas de resumen mat_* disponibles. Se re-evalúa cada 30 s; si el refresco SQL corrió, se descarta la caché."""
    if time.time() - _mat["checked"] < 30:
        return _mat["have"]
    try:
        have = frozenset(r["name"] for r in q(
            f"SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(N'{S}') AND name LIKE N'mat[_]%'", ttl=0))
        stamp = None
        if "mat_resumen_estado" in have:
            r = q(f"SELECT TOP 1 CONVERT(varchar(30), refrescado_en, 126) s FROM [{S}].[mat_resumen_estado] ORDER BY refrescado_en DESC", ttl=0)
            stamp = r[0]["s"] if r else None
    except Exception as e:  # nunca tumbar el dashboard por esto
        print("[mats] error:", e)
        _mat["checked"] = time.time()
        return _mat["have"]
    if stamp != _mat["stamp"] or have != _mat["have"]:
        _cache.clear()
        _catalog.update(ts=0.0, v=None)
        _filter_grid.update(ts=0.0, rows=None)
        _candidates.update(ts=0.0, rows=None)
        with _mjobs_lock:
            _mjobs.clear()
        _top10.update(rows=None, ts=0.0)
        try:
            TOP10_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    _mat.update(checked=time.time(), have=have, stamp=stamp)
    return have


def T(view: str) -> str:
    """vw_xxx -> mat_xxx si el resumen existe; si no, la vista original (mismas columnas, mismos resultados)."""
    mat = "mat_" + view[3:]
    return f"[{S}].[{mat if mat in mats() else view}]"


# Matriz de combinaciones realmente facturadas; se invalida con la publicacion SQL.
_filter_grid = {"ts": 0.0, "rows": None}
_filter_grid_lock = threading.RLock()


def filter_grid():
    with _filter_grid_lock:
        if _filter_grid["rows"] is not None and time.time() - _filter_grid["ts"] < CATALOG_TTL:
            return _filter_grid["rows"]
        rows = q(f"""SELECT DISTINCT period_start, zone_id, circuit_id, tariff_id, client_type
            FROM {T('vw_consumo_estimado_mes')}
            WHERE zone_id > 0 AND circuit_id > 0 AND tariff_id > 0""", ttl=CATALOG_TTL)
        _filter_grid.update(ts=time.time(), rows=rows)
        return rows


def matching_combinations(f, month=None, exclude=None):
    c = catalog()
    ids = f.ids
    if exclude == "region" and f.region and set(ids) == {z for z,v in c["zone_region"].items() if v == f.region}:
        ids = ()  # seleccion regional completa: la faceta puede ofrecer otras regiones
    return [r for r in filter_grid()
            if (exclude == "zones" or not ids or r["zone_id"] in ids)
            and (exclude == "region" or not f.region or c["zone_region"].get(r["zone_id"]) == f.region)
            and (exclude == "circuit" or f.circuit is None or r["circuit_id"] == f.circuit)
            and (exclude == "tariff" or f.tariff is None or r["tariff_id"] == f.tariff)
            and (exclude == "client_type" or not f.ctype or r["client_type"] == f.ctype)
            and (exclude == "month" or not month or str(r["period_start"])[:7] == month)]


_candidates = {"rows": None, "ts": 0.0, "loading": False}
_candidates_lock = threading.Lock()


def _candidate_job(stamp):
    try:
        rows = q(_top10_selection_sql().replace("TOP (10)", ""),
                 dict(top_all_zones=1, top_zones=[-1], top_circuit=None))
        if stamp == _mat["stamp"]:
            _candidates.update(rows=rows, ts=time.time())
    except Exception as exc:
        print("[ranking candidatos]", type(exc).__name__)
    finally:
        _candidates["loading"] = False


def selected_top10(ids, circuit):
    """Materializacion completa; en instalaciones anteriores calculo global compartido en segundo plano."""
    if "mat_criticidad_transformador_full" in mats():
        return q(_top10_selection_sql().replace(f"[{S}].[vw_criticidad_transformador]",
                  f"[{S}].[mat_criticidad_transformador_full]"),
                 dict(top_all_zones=0 if ids else 1, top_zones=list(ids) or [-1], top_circuit=circuit))
    with _candidates_lock:
        if not _candidates["loading"] and time.time() - _candidates["ts"] > TTL:
            _candidates["loading"] = True
            threading.Thread(target=_candidate_job, args=(_mat["stamp"],), daemon=True).start()
    rows = _candidates["rows"]
    if rows is None:
        return None  # la UI distingue calculando de un resultado vacio
    return [r for r in rows if (not ids or r["zone_id"] in ids)
            and (circuit is None or r["circuit_id"] == circuit)][:10]


# ---------------------------------------------------------------- Top 10 transformadores, todo el período (vista lenta)
_top10 = {"rows": None, "ts": 0.0, "loading": False, "thread": None}
_top10_lock = threading.Lock()


def _top10_worker():
    try:
        oid = q(f"SELECT OBJECT_ID(N'{S}.mat_top10_transformadores') AS oid", ttl=0)[0]["oid"]
        src = "mat_top10_transformadores" if oid else "vw_top10_transformadores"
        rows = q(f"""SELECT t.ranking, t.transformer_id, t.zone_id, t.circuit_id, t.transformer_name, t.score_criticidad score, t.beneficio_usd_recuperable usd,
            t.perdida_pct, t.morosidad_pct, t.componente_dominante causa, t.kpi_seguimiento kpi
            FROM [{S}].[{src}] t ORDER BY t.ranking""", ttl=0)
        _top10.update(rows=rows, ts=time.time())
        try:
            TOP10_FILE.parent.mkdir(exist_ok=True)
            TOP10_FILE.write_text(json.dumps({"ts": _top10["ts"], "rows": rows}), encoding="utf-8")
        except OSError:
            pass
    except Exception as e:  # no tumbar el dashboard por esta vista
        print("[top10] error:", e)
    finally:
        _top10["loading"] = False


def get_top10(wait: float = 3.0):
    """Devuelve filas del Top 10 (posiblemente de caché) o None si aún se está calculando."""
    if _top10["rows"] is None and TOP10_FILE.exists():
        try:
            d = json.loads(TOP10_FILE.read_text(encoding="utf-8"))
            if not d["rows"] or "transformer_id" in d["rows"][0]:   # caché de disco anterior (sin transformer_id): se descarta
                _top10.update(rows=d["rows"], ts=d["ts"])
        except (OSError, ValueError, KeyError):
            pass
    fresh = _top10["rows"] is not None and time.time() - _top10["ts"] < TOP10_TTL
    if not fresh:
        with _top10_lock:
            if not _top10["loading"]:
                _top10["loading"] = True
                t = threading.Thread(target=_top10_worker, daemon=True)
                _top10["thread"] = t
                t.start()
        if _top10["rows"] is None and _top10["thread"]:
            _top10["thread"].join(wait)  # si hay tabla materializada responde casi al instante
    return _top10["rows"]


def _top10_selection_sql() -> str:
    """Ranking global conservado; zona/circuito se aplican ANTES de TOP (10).

    La vista existente contiene todos los transformadores. Solo los valores se
    enlazan como parametros; nunca se concatena una zona/circuito en el SQL.
    """
    return f"""SELECT TOP (10) c.ranking, c.transformer_id, c.zone_id, c.circuit_id,
        c.transformer_name, c.score_criticidad score, c.impacto_usd usd,
        c.perdida_pct, c.morosidad_pct, d.comp causa,
        CASE d.comp WHEN N'Impacto económico' THEN N'USD de pérdida evitable mensual'
             WHEN N'Pérdida relativa' THEN N'% pérdida total mensual del transformador'
             WHEN N'Morosidad' THEN N'% cartera > 60 días'
             ELSE N'SAIDI / SAIFI del transformador' END kpi
        FROM [{S}].[vw_criticidad_transformador] c
        CROSS JOIN [{S}].[cfg_parametros] p
        CROSS APPLY (SELECT TOP (1) comp FROM (VALUES
            (N'Impacto económico', p.w_impacto * ISNULL(c.norm_impacto, 0)),
            (N'Pérdida relativa', p.w_perdida * ISNULL(c.norm_perdida, 0)),
            (N'Morosidad', p.w_morosidad * ISNULL(c.norm_morosidad, 0)),
            (N'Calidad de servicio', p.w_calidad * c.norm_calidad)) v(comp, aporte)
            ORDER BY aporte DESC, comp) d
        WHERE p.id = 1 AND (:top_all_zones = 1 OR c.zone_id IN (:top_zones))
          AND (:top_circuit IS NULL OR c.circuit_id = :top_circuit)
        ORDER BY c.score_criticidad DESC, c.transformer_id"""


# ---------------------------------------------------------------- Trabajo pesado por MES (en segundo plano, con caché)
# La consulta transformador x mes es pesada: se calcula una vez por mes en segundo plano y queda en memoria.
# Una sola pasada por mes alimenta: (1) el ranking de transformadores del mes (misma fórmula que
# vw_criticidad_transformador / vw_top10_transformadores, validada contra mat_top10_transformadores) y
# (2) el control "Suma de zonas = total del sistema". Se calcula una vez por mes y se guarda en memoria.
def _tr_sql() -> str:
    """Lote SQL parametrizado (:a/:b = límites del mes, :y/:m = año y mes)."""
    return f"""SET NOCOUNT ON;
CREATE TABLE #pt (transformer_id int, zone_id int, injected_kwh float, perdida_total_kwh float, perdida_evitable_usd float);
INSERT INTO #pt SELECT transformer_id, zone_id, injected_kwh, perdida_total_kwh, perdida_evitable_usd
  FROM {T('vw_perdida_transformador_mes')} WHERE period_year = :y AND period_month = :m;
WITH p AS (SELECT * FROM [{S}].[cfg_parametros] WHERE id = 1),
l AS (SELECT transformer_id, SUM(injected_kwh) injected_kwh, SUM(perdida_total_kwh) perdida_kwh, SUM(perdida_evitable_usd) impacto_usd
      FROM #pt WHERE transformer_id > 0 GROUP BY transformer_id),
m AS (SELECT c.transformer_id, SUM(s.balance) saldo_total, SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) saldo_mayor_60
      FROM [{S}].[fact_cartera_snapshot] s JOIN [{S}].[dim_cliente] c ON c.client_id = s.client_id
      WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM [{S}].[fact_cartera_snapshot] WHERE snapshot_date >= :a AND snapshot_date < :b)
      GROUP BY c.transformer_id),
cs AS (SELECT transformer_id, SUM(clientes_servidos) clientes_servidos FROM {T('vw_clientes_servidos')} GROUP BY transformer_id),
q AS (SELECT transformer_id, COUNT(*) eventos, SUM(cliente_minutos) cm, SUM(customers_affected) ci
      FROM {T('vw_interrupciones_detalle')} WHERE period_start >= :a AND period_start < :b GROUP BY transformer_id),
raw AS (SELECT t.transformer_id, t.transformer_name, t.zone_id, t.circuit_id, l.impacto_usd, l.perdida_kwh / NULLIF(l.injected_kwh, 0) perdida_pct,
        m.saldo_mayor_60 / NULLIF(m.saldo_total, 0) morosidad_pct,
        CAST(ISNULL(q.cm, 0) AS FLOAT) / NULLIF(cs.clientes_servidos, 0) saidi_min, CAST(ISNULL(q.ci, 0) AS FLOAT) / NULLIF(cs.clientes_servidos, 0) saifi
        FROM [{S}].[dim_transformador] t JOIN l ON l.transformer_id = t.transformer_id LEFT JOIN m ON m.transformer_id = t.transformer_id
        LEFT JOIN cs ON cs.transformer_id = t.transformer_id LEFT JOIN q ON q.transformer_id = t.transformer_id WHERE t.transformer_id > 0),
nrm AS (SELECT raw.*,
  100.0 * (impacto_usd - MIN(impacto_usd) OVER ()) / NULLIF(MAX(impacto_usd) OVER () - MIN(impacto_usd) OVER (), 0) norm_impacto,
  100.0 * (perdida_pct - MIN(perdida_pct) OVER ()) / NULLIF(MAX(perdida_pct) OVER () - MIN(perdida_pct) OVER (), 0) norm_perdida,
  100.0 * (morosidad_pct - MIN(morosidad_pct) OVER ()) / NULLIF(MAX(morosidad_pct) OVER () - MIN(morosidad_pct) OVER (), 0) norm_morosidad,
  100.0 * (saidi_min - MIN(saidi_min) OVER ()) / NULLIF(MAX(saidi_min) OVER () - MIN(saidi_min) OVER (), 0) norm_saidi,
  100.0 * (saifi - MIN(saifi) OVER ()) / NULLIF(MAX(saifi) OVER () - MIN(saifi) OVER (), 0) norm_saifi FROM raw),
sc AS (SELECT nrm.*, p.w_saidi_en_calidad * ISNULL(norm_saidi, 0) + (1 - p.w_saidi_en_calidad) * ISNULL(norm_saifi, 0) norm_calidad FROM nrm CROSS JOIN p),
fin AS (SELECT sc.*, p.w_impacto * ISNULL(norm_impacto, 0) + p.w_perdida * ISNULL(norm_perdida, 0)
        + p.w_morosidad * ISNULL(norm_morosidad, 0) + p.w_calidad * norm_calidad score FROM sc CROSS JOIN p),
rk AS (SELECT fin.*, RANK() OVER (ORDER BY score DESC) ranking FROM fin)
SELECT rk.ranking, rk.transformer_id, rk.zone_id, rk.circuit_id, rk.transformer_name, rk.score, rk.impacto_usd usd, rk.perdida_pct, rk.morosidad_pct,
  d.comp causa,
  CASE d.comp WHEN N'Impacto económico' THEN N'USD de pérdida evitable mensual' WHEN N'Pérdida relativa' THEN N'% pérdida total mensual del transformador'
              WHEN N'Morosidad' THEN N'% cartera > 60 días' ELSE N'SAIDI / SAIFI del transformador' END kpi,
  (SELECT SUM(perdida_total_kwh) FROM #pt) perdida_sistema
FROM rk CROSS JOIN p
CROSS APPLY (SELECT TOP 1 comp FROM (VALUES (N'Impacto económico', p.w_impacto * ISNULL(rk.norm_impacto, 0)), (N'Pérdida relativa', p.w_perdida * ISNULL(rk.norm_perdida, 0)),
        (N'Morosidad', p.w_morosidad * ISNULL(rk.norm_morosidad, 0)), (N'Calidad de servicio', p.w_calidad * rk.norm_calidad)) v(comp, aporte) ORDER BY aporte DESC) d
ORDER BY rk.ranking"""


def _month_job(bounds, st):
    try:
        a, b = bounds
        text, params = bind(_tr_sql(), dict(a=a, b=b, y=a.year, m=a.month))
        rows = _exec(text, params)
        st["sys"] = rows[0]["perdida_sistema"] if rows else None
        st["rows"] = [{k: v for k, v in r.items() if k != "perdida_sistema"} for r in rows]
        st["ctrl"] = q(ctrl_month_sql(), dict(a=a, b=b, y=a.year, m=a.month))
        st["err"] = False
    except Exception as e:
        print("[month_job] error:", e)
        st["sys"], st["rows"], st["err"] = None, [], True
    finally:
        st["ts"] = time.time()
        st["loading"] = False


def month_heavy(month, bounds) -> dict:
    """Estado del trabajo pesado del mes; lo lanza en segundo plano la primera vez (o tras un error de hace >60 s)."""
    with _mjobs_lock:
        st = _mjobs.get(month)
        if st is not None and st["err"] and time.time() - st["ts"] > 60:
            st = None
        if st is None:
            if len(_mjobs) >= 36:
                _mjobs.clear()
            st = {"rows": None, "sys": None, "loading": True, "ts": 0.0, "err": False, "ctrl": None}
            _mjobs[month] = st
            threading.Thread(target=_month_job, args=(bounds, st), daemon=True).start()
        return st


def _warm():
    try:
        get_top10(wait=0)
        selected_top10([], None)
        _dash(None, None, None, None, None, None)  # precalienta la caché con "todas las zonas" y "todo el período"
    except Exception as e:
        print("[warmup] error:", e)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(title="EnerGrid BI Web", lifespan=lifespan)

@app.middleware("http")
async def timing(request, call_next):
    started = time.perf_counter()
    metrics = []
    token = _metrics.set(metrics)
    try:
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f"app;dur={elapsed:.2f}, sql_sum;dur={sum(metrics):.2f}, sql_max;dur={max(metrics, default=0):.2f}"
        response.headers["X-SQL-Queries"] = str(len(metrics))
        response.headers["X-EnerGrid-Build"] = "rendimiento-20261025"
        return response
    finally:
        _metrics.reset(token)



@app.get("/api/zones")
def zones():
    return q(f"SELECT zone_id, zone_name, region FROM [{S}].[dim_zona] WHERE zone_id > 0 ORDER BY zone_name")


@app.get("/api/data-status")
def data_status():
    """Solo lectura. «Datos actualizados al» (mat_resumen_estado), estado de la última ejecución y SLA por fuente (propuesta hasta aprobarse).
    Ver estado_datos.py y docs/SLA_ACTUALIZACION_DATOS.md. No ejecuta el refresco ni escribe en SQL Server.
    Si el refresco lanzado desde la web sigue en marcha, informa 'en_curso' aunque SQL Server aún no haya registrado
    la fila EN_CURSO (el hilo de fondo tarda un instante en conectar); así el botón no se reactiva antes de tiempo."""
    st = estado_datos(q)
    if _refresh_running and st.get("estado") != "en_curso":
        st = {**st, "estado": "en_curso", "estado_texto": TEXTOS["en_curso"]}
    return st


_refresh_lock = threading.Lock()
_refresh_running = False


def _run_refresh_bg():
    global _refresh_running
    try:
        con = pyodbc.connect(conn_str(), autocommit=True, timeout=30)
        con.timeout = 3600
        try:
            cur = con.cursor()
            cur.execute("SET NOCOUNT ON; EXEC energrid.usp_refrescar_resumenes_log @origen = N'WebDashboard';")
        finally:
            con.close()
    except Exception as e:
        print("[refresh] error al ejecutar refresco:", e)
    finally:
        with _refresh_lock:
            _refresh_running = False
        # Revalidar ya (sin esperar el sondeo de 30 s): el refresco acaba de publicar tablas nuevas y hay que vaciar la caché.
        _mat["checked"] = 0.0
        mats()


@app.post("/api/refresh")
def trigger_refresh():
    """Inicia en segundo plano la reconstrucción de los resúmenes precalculados (usp_refrescar_resumenes_log).
    Exclusión mutua protegida por cerrojo en memoria y sp_getapplock en SQL Server."""
    global _refresh_running
    with _refresh_lock:
        if _refresh_running:
            return JSONResponse(
                {"status": "en_curso", "mensaje": "Ya hay una actualización de resúmenes en ejecución."},
                status_code=409,
            )
        st = estado_datos(q)
        if st.get("estado") == "en_curso":
            return JSONResponse(
                {"status": "en_curso", "mensaje": "Ya hay una actualización de resúmenes en curso en la base de datos."},
                status_code=409,
            )
        _refresh_running = True
        threading.Thread(target=_run_refresh_bg, daemon=True).start()

    return JSONResponse(
        {"status": "iniciado", "mensaje": "Actualización de resúmenes iniciada en segundo plano."},
        status_code=202,
    )



@app.get("/api/filters")
def filters(zones: Optional[str] = Query(None, max_length=400), month: Optional[str] = Query(None, max_length=7),
            region: Optional[str] = Query(None, max_length=40), circuit: Optional[int] = Query(None, ge=0),
            tariff: Optional[int] = Query(None, ge=1), client_type: Optional[str] = Query(None, max_length=40)):
    """Facetas: cada opcion ignora su propio filtro y respeta los demas."""
    try:
        _month_bounds(month)
        f = resolve(zones, region, circuit, tariff, client_type)
        c = catalog()
        facets = {key: matching_combinations(f, month, key) for key in
                  ("region", "zones", "circuit", "tariff", "client_type", "month")}
        region_ids = {c["zone_region"].get(r["zone_id"]) for r in facets["region"]}
        zone_ids = {r["zone_id"] for r in facets["zones"]}
        circuit_ids = {r["circuit_id"] for r in facets["circuit"]}
        tariff_ids = {r["tariff_id"] for r in facets["tariff"]}
        return dict(regions=[r for r in c["regions"] if r in region_ids],
                    zones=[dict(zone_id=z, region=c["zone_region"][z]) for z in sorted(zone_ids)],
                    circuits=[r for r in c["circuits"] if r["circuit_id"] in circuit_ids],
                    tariffs=[r for r in c["tariffs"] if r["tariff_id"] in tariff_ids],
                    client_types=sorted({r["client_type"] for r in facets["client_type"]}),
                    months=sorted({str(r["period_start"])[:7] for r in facets["month"]}),
                    valid=bool(matching_combinations(f, month)), dim_ready=DIM_TABLES <= mats())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        return JSONResponse({"error": "No se pudieron cargar opciones dependientes. Revisa conexion y resumenes."}, status_code=503)


@app.get("/api/map/zones")
def map_zones():
    """Centroides orientativos de clientes (no polígonos); datos de solo lectura y agregados."""
    return q(f"""
        SELECT c.zone_id, COALESCE(z.zone_name, CONCAT('Zona ', c.zone_id)) AS zone_name,
               COUNT_BIG(*) AS client_count,
               CAST(AVG(CAST(c.latitude AS float)) AS float) AS latitude,
               CAST(AVG(CAST(c.longitude AS float)) AS float) AS longitude
        FROM [{S}].[dim_cliente] c
        LEFT JOIN [{S}].[dim_zona] z ON z.zone_id = c.zone_id
        WHERE c.client_id > 0 AND c.zone_id > 0 AND c.active = 1
          AND c.latitude BETWEEN -90 AND 90 AND c.longitude BETWEEN -180 AND 180
          AND NOT (c.latitude = 0 AND c.longitude = 0)
        GROUP BY c.zone_id, z.zone_name
        ORDER BY c.zone_id
    """, ttl=CATALOG_TTL)


@app.get("/api/months")
def months():
    """Meses CON DATOS (tabla intermedia de pérdida). dim_tiempo trae también meses futuros sin datos (ene-mar 2026)."""
    return q(f"SELECT DISTINCT YEAR(period_start) [year], MONTH(period_start) [month] FROM {T('vw_perdida_zona_mes')} ORDER BY [year], [month]", ttl=CATALOG_TTL)


def _month_bounds(month: Optional[str]):
    """Validar AAAA-MM y devolver límites como fechas (inicio inclusivo, fin exclusivo); None = todo el período."""
    if not month:
        return None
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("El mes debe tener formato AAAA-MM")
    y, m = map(int, month.split("-"))
    ey, em = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.date(y, m, 1), dt.date(ey, em, 1)


def crit_sql(bounds, last_cut):
    """Criticidad de TODAS las zonas (alimenta 'Top 10 zonas por criticidad', 'SAIDI por zona' y el color del mapa).

    Se devuelven siempre las 15 zonas: la normalización min-max se hace entre todas y el frontend filtra la selección,
    de modo que la misma consulta (en caché) sirve para cualquier combinación de zonas/región.
    Todo el período: tabla/vista anual existente (sin cambios). Un mes: MISMA fórmula que energrid.vw_criticidad_zona
    leyendo solo ese mes de las tablas intermedias (mat_perdida_zona_mes, mat_dso_zona_mes en el último corte del mes)
    y SAIDI/SAIFI del mes. Verificado: con el año completo reproduce mat_criticidad_zona con diferencia 0.
    La criticidad es por ZONA completa: no se recalcula con circuito, tarifa ni tipo de cliente.
    """
    cols = ("zone_id, zone_name, region, score, perdida_pct, morosidad_pct, saidi_min, saifi, impacto_usd, "
            "norm_impacto, norm_perdida, norm_morosidad, norm_saidi, norm_saifi")
    if bounds is None:
        return (f"SELECT zone_id, zone_name, region, score_criticidad score, perdida_pct, morosidad_pct, saidi_min, saifi, impacto_usd, "
                f"norm_impacto, norm_perdida, norm_morosidad, norm_saidi, norm_saifi FROM {T('vw_criticidad_zona')} ORDER BY score DESC")
    return f"""WITH p AS (SELECT * FROM [{S}].[cfg_parametros] WHERE id = 1),
        l AS (SELECT zone_id, SUM(injected_kwh) injected_kwh, SUM(perdida_total_kwh) perdida_kwh, SUM(perdida_evitable_usd) impacto_usd
              FROM {T('vw_perdida_zona_mes')} WHERE period_start >= :a AND period_start < :b GROUP BY zone_id),
        m AS (SELECT zone_id, saldo_total, saldo_mayor_60 FROM {T('vw_dso_zona_mes')} WHERE snapshot_date = {last_cut}),
        q AS (SELECT zone_id, SUM(cliente_minutos) cm, SUM(clientes_interrumpidos) ci, MAX(clientes_servidos) cs
              FROM {T('vw_saidi_saifi_zona_mes')} WHERE period_start >= :a AND period_start < :b GROUP BY zone_id),
        raw AS (SELECT z.zone_id, z.zone_name, z.region, l.impacto_usd, l.perdida_kwh / NULLIF(l.injected_kwh, 0) perdida_pct,
                m.saldo_mayor_60 / NULLIF(m.saldo_total, 0) morosidad_pct,
                CAST(q.cm AS FLOAT) / NULLIF(q.cs, 0) saidi_min, CAST(q.ci AS FLOAT) / NULLIF(q.cs, 0) saifi
                FROM [{S}].[dim_zona] z JOIN l ON l.zone_id = z.zone_id LEFT JOIN m ON m.zone_id = z.zone_id
                LEFT JOIN q ON q.zone_id = z.zone_id WHERE z.zone_id > 0),
        nrm AS (SELECT raw.*,
            100.0 * (impacto_usd - MIN(impacto_usd) OVER ()) / NULLIF(MAX(impacto_usd) OVER () - MIN(impacto_usd) OVER (), 0) norm_impacto,
            100.0 * (perdida_pct - MIN(perdida_pct) OVER ()) / NULLIF(MAX(perdida_pct) OVER () - MIN(perdida_pct) OVER (), 0) norm_perdida,
            100.0 * (morosidad_pct - MIN(morosidad_pct) OVER ()) / NULLIF(MAX(morosidad_pct) OVER () - MIN(morosidad_pct) OVER (), 0) norm_morosidad,
            100.0 * (saidi_min - MIN(saidi_min) OVER ()) / NULLIF(MAX(saidi_min) OVER () - MIN(saidi_min) OVER (), 0) norm_saidi,
            100.0 * (saifi - MIN(saifi) OVER ()) / NULLIF(MAX(saifi) OVER () - MIN(saifi) OVER (), 0) norm_saifi FROM raw),
        sc AS (SELECT nrm.*, p.w_saidi_en_calidad * ISNULL(norm_saidi, 0) + (1 - p.w_saidi_en_calidad) * ISNULL(norm_saifi, 0) norm_calidad
               FROM nrm CROSS JOIN p),
        fin AS (SELECT sc.*, p.w_impacto * ISNULL(norm_impacto, 0) + p.w_perdida * ISNULL(norm_perdida, 0)
                + p.w_morosidad * ISNULL(norm_morosidad, 0) + p.w_calidad * norm_calidad AS score FROM sc CROSS JOIN p)
        SELECT {cols} FROM fin ORDER BY score DESC"""


# ---------------------------------------------------------------- Matriz de control y reconciliación
CTRL_NAMES = (
    "Energía inyectada: vista = hecho diario",
    "kWh facturados: vista = hecho de facturación (sin doble conteo)",
    "Identidad: pérdida total = técnica + no técnica + brecha de medición",
    "Suma de zonas = total del sistema (kWh perdidos)",
    "Cartera al corte = facturado - pagos (facturas con saldo > 0)",
    "Clientes interrumpidos (SAIFI): vista de zona = detalle",
)


def ctrl_month_sql() -> str:
    """Mismos 6 controles que vw_control_reconciliacion, acotados al mes (:a/:b/:y/:m)."""
    pz, rng = T("vw_perdida_zona_mes"), "period_start >= :a AND period_start < :b"
    return f"""DECLARE @cut DATE = (SELECT MAX(snapshot_date) FROM [{S}].[fact_cartera_snapshot] WHERE snapshot_date >= :a AND snapshot_date < :b);
SELECT
 (SELECT SUM(injected_kwh) FROM [{S}].[fact_energia_transformador_diaria] WHERE [date] >= :a AND [date] < :b) e1,
 (SELECT SUM(injected_kwh) FROM {pz} WHERE {rng}) o1,
 (SELECT SUM(billed_kwh) FROM [{S}].[fact_facturacion] WHERE period_year = :y AND period_month = :m) e2,
 (SELECT SUM(billed_kwh) FROM {pz} WHERE {rng}) o2,
 (SELECT SUM(perdida_total_kwh - perdida_tecnica_kwh - perdida_no_tecnica_kwh - brecha_medicion_kwh) FROM {pz} WHERE {rng}) o3,
 (SELECT SUM(perdida_total_kwh) FROM {pz} WHERE {rng}) o4,
 (SELECT SUM(CASE WHEN x.s > 0 THEN x.s ELSE 0 END) FROM (SELECT f.amount_due - ISNULL(SUM(pg.payment_amount), 0) s
     FROM [{S}].[fact_facturacion] f LEFT JOIN [{S}].[fact_pago] pg ON pg.invoice_id = f.invoice_id AND pg.payment_date <= @cut
     WHERE f.invoice_date <= @cut GROUP BY f.invoice_id, f.amount_due) x) e5,
 (SELECT SUM(balance) FROM [{S}].[fact_cartera_snapshot] WHERE snapshot_date = @cut) o5,
 (SELECT SUM(customers_affected) FROM {T('vw_interrupciones_detalle')} WHERE {rng}) e6,
 (SELECT SUM(clientes_interrumpidos) FROM {T('vw_saidi_saifi_zona_mes')} WHERE {rng}) o6"""


def ctrl_month_rows(r: dict, st: dict) -> list[dict]:
    num = lambda v: float(v or 0)

    def row(i, e, o, tol, pending=False):
        ok = None if (pending or e is None) else abs(num(e) - num(o)) <= tol
        return dict(control=CTRL_NAMES[i], esperado=e, obtenido=o, ok=ok, pending=pending)

    done = st["rows"] is not None
    e4 = st["sys"] if done else None
    return [row(0, r["e1"], r["o1"], 1.0), row(1, r["e2"], r["o2"], 1.0), row(2, 0, r["o3"], 1.0),
            row(3, e4, r["o4"], 1.0, pending=not done), row(4, r["e5"], r["o5"], 1.0), row(5, r["e6"], r["o6"], 0.0)]


@app.get("/api/dashboard")
def dashboard(zones: Optional[str] = Query(None, max_length=400), month: Optional[str] = Query(None, max_length=7),
              region: Optional[str] = Query(None, max_length=40), circuit: Optional[int] = Query(None, ge=0, le=10_000_000),
              tariff: Optional[int] = Query(None, ge=-1, le=10_000_000), client_type: Optional[str] = Query(None, max_length=40)):
    return _dash(zones, month, region, circuit, tariff, client_type)


def _dash(zones, month, region, circuit, tariff, client_type):
    try:
        bounds = _month_bounds(month)
        f = resolve(zones, region, circuit, tariff, client_type)
    except ValueError as exc:                      # incluye FilterError
        return JSONResponse({"error": str(exc)}, status_code=422)
    ids, circ, tar, ctp = f.ids, f.circuit, f.tariff, f.ctype
    hasC, hasT, hasK = circ is not None, tar is not None, ctp is not None
    dimc = hasC or hasT or hasK                    # algún filtro que exige las tablas con dimensiones completas
    is_partial = bounds is not None
    try:
        if not matching_combinations(f, month):
            return JSONResponse({"error": "La combinacion de periodo, region, zona, circuito, tarifa y tipo no tiene clientes facturados. Ajusta los filtros."}, status_code=400)
    except Exception:
        return JSONResponse({"error": "No se pudo verificar la matriz de filtros; comprueba conexion y resumen de facturas."}, status_code=503)

    vals: dict = {}
    if ids: vals["z"] = list(ids)
    if hasC: vals["c"] = circ
    if hasT: vals["t"] = tar
    if hasK: vals["k"] = ctp
    if bounds: vals.update(a=bounds[0], b=bounds[1], y=bounds[0].year, m=bounds[0].month)

    # Condiciones reutilizables ("" si el filtro no está activo) y armado de WHERE/AND.
    cz = lambda col="zone_id": f"{col} IN (:z)" if ids else ""
    cc = lambda col="circuit_id": f"{col} = :c" if hasC else ""
    ct = lambda col="tariff_id": f"{col} = :t" if hasT else ""
    ck = lambda col="client_type": f"{col} = :k" if hasK else ""
    cm = lambda col="period_start": f"{col} >= :a AND {col} < :b" if bounds else ""

    def wh(*parts, pre="WHERE"):
        ps = [p for p in parts if p]
        return f"{pre} " + " AND ".join(ps) if ps else ""

    M = mats()  # tablas de resumen disponibles (una consulta cada 30 s como máximo)
    need = set()
    if hasC: need |= {"mat_perdida_circuito_mes", "mat_cobranza_dim_mes"}
    if dimc: need |= {"mat_cartera_dim", "mat_facturado_dim"}
    if hasT or hasK: need |= {"mat_ami_dim_mes"}
    missing = sorted(need - M)
    if missing:
        return JSONResponse({"error": "Faltan tablas de resumen para filtrar por circuito, tarifa o tipo de cliente: "
                                      + ", ".join(missing) + ". Aplica sql/04_procedimientos.sql y sql/05_resumenes_sla.sql; ejecuta "
                                      "EXEC energrid.usp_refrescar_resumenes_log; (los filtros de región, zona y mes no la necesitan).",
                             "missing": missing}, status_code=409)

    # Snapshot de cartera: último corte disponible dentro del mes elegido (o el último de todo el período).
    last_cut = f"(SELECT MAX(snapshot_date) FROM {T('vw_dso_zona_mes')})" if bounds is None else (
        f"(SELECT MAX(snapshot_date) FROM {T('vw_dso_zona_mes')} WHERE snapshot_date >= :a AND snapshot_date < :b)")
    st = month_heavy(month, bounds) if is_partial else None  # arranca en segundo plano mientras corren las demás consultas

    try:
        # ---------- Fuentes por indicador (qué tabla responde a qué filtro) ----------
        LOSS = f"[{S}].[mat_perdida_circuito_mes]" if hasC else T("vw_perdida_zona_mes")      # región/zona/circuito (no tarifa/tipo)
        AMI = f"[{S}].[mat_ami_dim_mes]" if (hasT or hasK) else T("vw_ami_mes")              # + tarifa/tipo con tabla dim
        COB = f"[{S}].[mat_cobranza_dim_mes]" if hasC else T("vw_cobranza_mes")              # circuito exige tabla dim
        EST = T("vw_consumo_estimado_mes")                                                    # ya trae todas las dimensiones
        # AMI / CE / consumo estimado aceptan zona+circuito+tarifa+tipo (si hay tarifa/tipo/circuito se usa la tabla que trae esas columnas)
        w_loss, w_all = wh(cz(), cc()), wh(cz(), cc(), ct(), ck())

        if dimc:
            CD, FD = f"[{S}].[mat_cartera_dim]", f"[{S}].[mat_facturado_dim]"
            fcd = wh(cz(), cc(), ct(), ck(), pre="AND")
            aging = f"""SELECT aging_bucket, aging_order, SUM(saldo) saldo, SUM(facturas) facturas
                FROM {CD} WHERE snapshot_date={last_cut} {fcd} GROUP BY aging_bucket, aging_order ORDER BY aging_order"""
            seg = f"""SELECT client_type, COALESCE(segmento_consumo,'Sin clasificar') segmento, SUM(saldo) total, SUM(saldo_mayor_60) m60
                FROM {CD} WHERE snapshot_date={last_cut} {fcd} GROUP BY client_type, segmento_consumo ORDER BY total DESC"""
            cart_sql = f"""WITH s AS (SELECT SUM(saldo) total, SUM(saldo_mayor_60) m60 FROM {CD} WHERE snapshot_date={last_cut} {fcd}),
                v AS (SELECT SUM(facturado_ventana) fv, MAX(dias_ventana) dv FROM {FD} WHERE snapshot_date={last_cut} {fcd})
                SELECT s.total, s.m60, {last_cut} corte, s.m60*1.0/NULLIF(s.total,0) pct_m60,
                       s.total/NULLIF(v.fv/NULLIF(v.dv,0),0) dso FROM s CROSS JOIN v"""
            fz = wh(cc("d.circuit_id"), ct("d.tariff_id"), ck("d.client_type"), pre="AND")
            m60z = f"""SELECT d.zone_id, COALESCE(z.zone_name,CONCAT('Zona ',d.zone_id)) zone_name, SUM(d.saldo) saldo_total,
                SUM(d.saldo_mayor_60) saldo_mayor_60, SUM(d.saldo_mayor_60)*1.0/NULLIF(SUM(d.saldo),0) pct, CAST(NULL AS float) dso
                FROM {CD} d LEFT JOIN [{S}].[dim_zona] z ON z.zone_id=d.zone_id
                WHERE d.snapshot_date={last_cut} AND d.zone_id>0 {fz} GROUP BY d.zone_id, z.zone_name ORDER BY pct DESC"""
        else:
            A = wh(cz(), pre="AND")
            if "mat_cartera_resumen" in M:
                # Resumen de cartera (snapshot x zona x tipo x tramo x segmento): mismas sumas que la vista fila a fila.
                aging = f"""SELECT aging_bucket, aging_order, SUM(saldo) saldo, SUM(facturas) facturas
                    FROM [{S}].[mat_cartera_resumen] WHERE snapshot_date={last_cut} {A} GROUP BY aging_bucket, aging_order ORDER BY aging_order"""
                seg = f"""SELECT client_type, COALESCE(segmento_consumo,'Sin clasificar') segmento, SUM(saldo) total, SUM(saldo_mayor_60) m60
                    FROM [{S}].[mat_cartera_resumen] WHERE snapshot_date={last_cut} {A}
                    GROUP BY client_type, segmento_consumo ORDER BY total DESC"""
            else:
                aging = f"""SELECT aging_bucket, aging_order, SUM(balance) saldo, COUNT(*) facturas
                    FROM [{S}].[vw_cartera_snapshot] WHERE snapshot_date={last_cut} {A} GROUP BY aging_bucket, aging_order ORDER BY aging_order"""
                seg = f"""SELECT client_type, COALESCE(segmento_consumo,'Sin clasificar') segmento, SUM(balance) total,
                    SUM(CASE WHEN es_mayor_60=1 THEN balance ELSE 0 END) m60
                    FROM [{S}].[vw_cartera_snapshot] WHERE snapshot_date={last_cut} {A}
                    GROUP BY client_type, segmento_consumo ORDER BY total DESC"""
            cart_sql = f"""SELECT SUM(saldo_total) total, SUM(saldo_mayor_60) m60, MAX(snapshot_date) corte,
                SUM(saldo_mayor_60)*1.0/NULLIF(SUM(saldo_total),0) pct_m60,
                SUM(saldo_total)/NULLIF(SUM(facturado_ventana)/NULLIF(MAX(dias_ventana),0),0) dso
                FROM {T('vw_dso_zona_mes')} WHERE snapshot_date={last_cut} {A}"""
            m60z = f"""SELECT d.zone_id, COALESCE(z.zone_name,CONCAT('Zona ',d.zone_id)) zone_name, d.saldo_total, d.saldo_mayor_60,
                d.pct_cartera_mayor_60 pct, d.dso_dias dso FROM {T('vw_dso_zona_mes')} d
                LEFT JOIN [{S}].[dim_zona] z ON z.zone_id=d.zone_id
                WHERE d.snapshot_date={last_cut} AND d.zone_id>0 ORDER BY pct DESC"""

        # Cartera total y >60 días por tarifa: respeta zona/región, circuito, tarifa y tipo.
        cart_tar = None
        if "mat_cartera_dim" in M:
            cart_tar = f"""SELECT cd.tariff_id, COALESCE(t.tariff_name, CONCAT('Tarifa ', cd.tariff_id)) tarifa, SUM(cd.saldo) total,
                SUM(cd.saldo_mayor_60) m60, SUM(cd.saldo_mayor_60)*1.0/NULLIF(SUM(cd.saldo),0) pct
                FROM [{S}].[mat_cartera_dim] cd LEFT JOIN [{S}].[dim_tarifa] t ON t.tariff_id = cd.tariff_id
                WHERE cd.snapshot_date={last_cut}
                {wh(cz("cd.zone_id"), cc("cd.circuit_id"), ct("cd.tariff_id"), ck("cd.client_type"), pre="AND")}
                GROUP BY cd.tariff_id, t.tariff_name ORDER BY total DESC"""

        # Confiabilidad: región/zona/circuito (las interrupciones no se miden por tarifa ni tipo de cliente).
        if hasC:
            # Circuito: directo del detalle de interrupciones; clientes servidos = clientes activos del circuito.
            rel_sql = f"""WITH ev AS (SELECT period_start, COUNT(*) eventos, SUM(customers_affected) ci, SUM(cliente_minutos) cm, SUM(duration_minutes) minutos
                    FROM {T('vw_interrupciones_detalle')} WHERE circuit_id = :c GROUP BY period_start),
                sv AS (SELECT COUNT(*) cs FROM [{S}].[dim_cliente] WHERE client_id > 0 AND active = 1 AND circuit_id = :c)
                SELECT ev.period_start, ev.cm, ev.ci, sv.cs, ev.eventos, ev.minutos,
                       ev.cm*1.0/NULLIF(sv.cs,0) saidi, ev.ci*1.0/NULLIF(sv.cs,0) saifi
                FROM ev CROSS JOIN sv ORDER BY ev.period_start"""
        else:
            rel_sql = f"""SELECT period_start, SUM(cliente_minutos) cm, SUM(clientes_interrumpidos) ci,
                SUM(clientes_servidos) cs, SUM(eventos) eventos, SUM(minutos) minutos,
                SUM(cliente_minutos)*1.0/NULLIF(SUM(clientes_servidos),0) saidi,
                SUM(clientes_interrumpidos)*1.0/NULLIF(SUM(clientes_servidos),0) saifi
                FROM {T('vw_saidi_saifi_zona_mes')} {wh(cz())} GROUP BY period_start ORDER BY period_start"""

        if bounds is None:
            rec_conds = ["es_recurrente=1"]
            if ids: rec_conds.append(f"circuit_id IN (SELECT circuit_id FROM [{S}].[dim_circuito] WHERE zone_id IN (:z))")
            if hasC: rec_conds.append("circuit_id = :c")
            recurr = f"""SELECT TOP 15 circuit_id, eventos, meses_con_evento, cliente_minutos, causa_principal
                FROM {T('vw_circuitos_recurrentes')} {wh(*rec_conds)} ORDER BY cliente_minutos DESC"""
            ctrl = f"SELECT control, esperado, obtenido, ok FROM {T('vw_control_reconciliacion')}"
        else:
            # La recurrencia (>= P90 y >= 6 meses) no aplica a un solo mes: se muestran los circuitos de mayor impacto del mes.
            recurr = f"""WITH base AS (SELECT circuit_id, cause_name, cliente_minutos, period_start FROM {T('vw_interrupciones_detalle')}
                    {wh("circuit_id > 0", cm(), cz(), cc())}),
                c AS (SELECT circuit_id, COUNT(*) eventos, COUNT(DISTINCT period_start) meses_con_evento, SUM(cliente_minutos) cliente_minutos
                      FROM base GROUP BY circuit_id),
                cc AS (SELECT circuit_id, cause_name, ROW_NUMBER() OVER (PARTITION BY circuit_id ORDER BY COUNT(*) DESC, cause_name) rn
                       FROM base GROUP BY circuit_id, cause_name)
                SELECT TOP 15 c.circuit_id, c.eventos, c.meses_con_evento, c.cliente_minutos, cc.cause_name causa_principal
                FROM c JOIN cc ON cc.circuit_id = c.circuit_id AND cc.rn = 1 ORDER BY c.cliente_minutos DESC"""
            ctrl = "SELECT 1 AS pending"

        # Alertas: las de SAIDI/AMI son por zona; con un circuito solo existen las de nivel circuito (pérdida anómala).
        al_c = "nivel = N'Circuito' AND entidad_id = :c" if hasC else ""
        qs = dict(
            params=f"""SELECT TOP 1 periodo_desde, periodo_hasta, fecha_corte, perdida_tecnica_ref, w_impacto, w_perdida,
                w_morosidad, w_calidad, alerta_zscore_perdida, alerta_saidi_deterioro, alerta_ami_min FROM [{S}].[cfg_parametros]""",
            # Series mensuales: periodo completo (solo filtran por zona/circuito) -> misma consulta en caché para cualquier mes.
            loss_month=f"""SELECT period_start, SUM(injected_kwh) injected_kwh, SUM(billed_kwh) billed_kwh,
                SUM(perdida_total_kwh) perdida_kwh, SUM(perdida_tecnica_kwh) tecnica, SUM(perdida_no_tecnica_kwh) no_tecnica,
                SUM(brecha_medicion_kwh) brecha, SUM(perdida_evitable_usd) evitable_usd,
                SUM(perdida_total_kwh)*1.0/NULLIF(SUM(injected_kwh),0) perdida_pct
                FROM {LOSS} {w_loss} GROUP BY period_start ORDER BY period_start""",
            # Rankings por zona: TODAS las zonas (filtra el frontend) y solo el mes elegido (y el circuito, si hay).
            loss_zone=f"""SELECT p.zone_id, COALESCE(z.zone_name,CONCAT('Zona ',p.zone_id)) zone_name,
                SUM(p.perdida_total_kwh)*1.0/NULLIF(SUM(p.injected_kwh),0) perdida_pct, SUM(p.perdida_evitable_usd) evitable_usd
                FROM {LOSS} p LEFT JOIN [{S}].[dim_zona] z ON z.zone_id=p.zone_id
                {wh(cm("p.period_start"), cc("p.circuit_id"))} GROUP BY p.zone_id, z.zone_name ORDER BY perdida_pct DESC""",
            ami=f"""SELECT period_start, SUM(lecturas_validas)*1.0/NULLIF(SUM(lecturas_esperadas),0) disponibilidad,
                SUM(lecturas_validas) validas, SUM(lecturas_esperadas) esperadas
                FROM {AMI} {w_all} GROUP BY period_start ORDER BY period_start""",
            # Disponibilidad AMI por ZONA del período (mes elegido o todo el período): alimenta tooltip y panel del mapa.
            # Única consulta nueva: `ami` agrupa solo por mes. Misma tabla (AMI) y misma fórmula que `ami` y vw_alertas.
            # Devuelve todas las zonas (como loss_zone y crit); respeta mes, circuito, tarifa y tipo, NO la selección de zonas.
            ami_zone=f"""SELECT zone_id, SUM(lecturas_validas)*1.0/NULLIF(SUM(lecturas_esperadas),0) disponibilidad,
                SUM(lecturas_validas) validas, SUM(lecturas_esperadas) esperadas
                FROM {AMI} {wh("zone_id > 0", cm(), cc(), ct(), ck())} GROUP BY zone_id ORDER BY zone_id""",
            est=f"""SELECT period_start, SUM(facturas_estimadas) est_n, SUM(facturas_emitidas) emit_n,
                SUM(facturas_estimadas)*1.0/NULLIF(SUM(facturas_emitidas),0) pct_estimadas
                FROM {EST} {w_all} GROUP BY period_start ORDER BY period_start""",
            cob=f"""SELECT period_start, SUM(exigible_usd) exigible, SUM(cobros_usd) cobros,
                SUM(cobros_usd)*1.0/NULLIF(SUM(exigible_usd),0) ce
                FROM {COB} {w_all} GROUP BY period_start ORDER BY period_start""",
            cart=cart_sql,
            aging=aging,
            m60_zone=m60z,
            seg=seg,
            rel_month=rel_sql,
            # Causas: directo del detalle de interrupciones (mismos totales que vw_saidi_saifi_circuito_causa, verificado).
            causes=f"""SELECT TOP 8 cause_name, SUM(cliente_minutos) cm, COUNT(*) eventos
                FROM {T('vw_interrupciones_detalle')} {wh(cz(), cc(), cm())} GROUP BY cause_name ORDER BY cm DESC""",
            recurr=recurr,
            crit=crit_sql(bounds, last_cut),
            al_sum=f"SELECT tipo_alerta, severidad, COUNT(*) n FROM {T('vw_alertas')} {wh(cz(), cm(), al_c)} GROUP BY tipo_alerta, severidad",
            al_svc=f"""SELECT TOP 20 tipo_alerta, nivel, entidad_id, severidad, meses_en_alerta
                FROM {T('vw_alertas')} {wh("tipo_alerta IN (N'Deterioro SAIDI', N'Disponibilidad AMI baja')", cz(), cm(), "1 = 0" if hasC else "")}
                ORDER BY severidad ASC, meses_en_alerta DESC""",
            ctrl=ctrl,
        )
        if is_partial:
            qs.pop("ctrl", None)
        if cart_tar:
            qs["cart_tarifa"] = cart_tar
        r = run_parallel(qs, vals)
        if is_partial:
            r["ctrl"] = st.get("ctrl") or []
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    params, loss_month, ami, est, cob = r["params"], r["loss_month"], r["ami"], r["est"], r["cob"]
    cart, rel_month = r["cart"], r["rel_month"]

    # Un mes: los KPI se calculan solo con las filas de ese mes de las series (todo el período: suma de todas).
    in_month = lambda rows: [x for x in rows if str(x["period_start"])[:7] == month] if month else rows
    lm_s, cob_s, ami_s, est_s, rel_s = map(in_month, (loss_month, cob, ami, est, rel_month))

    # Top 10 de transformadores y matriz de control (el ranking es global: se filtra por zona/región y circuito)
    t10f = lambda rows: [x for x in rows if (not ids or x["zone_id"] in ids) and (not hasC or x.get("circuit_id") == circ)]
    if is_partial:
        top10_loading = st["rows"] is None or not st.get("ctrl")
        top10 = t10f(st["rows"] or [])[:10]
        ctrl_rows = ctrl_month_rows(r["ctrl"][0], st) if r["ctrl"] else [
            dict(control=name, esperado=None, obtenido=None, ok=None, pending=True) for name in CTRL_NAMES]
    else:
        if ids or hasC:
            # Consulta de todos los candidatos -> WHERE -> ORDER BY -> TOP (10).
            top10 = selected_top10(ids, circ)
            top10_loading = top10 is None
            top10 = top10 or []
        else:
            top10 = get_top10()
            top10_loading = top10 is None
            top10 = top10 or []
        ctrl_rows = r["ctrl"]

    sm = lambda rows, k: sum((x[k] or 0) for x in rows)
    inj = sm(lm_s, "injected_kwh")
    c = cart[0] if cart else {}
    if c.get("corte") is None and is_partial:
        c = {}
    cm_ = sm(rel_s, "cm")
    cs_sys = max((x["cs"] or 0) for x in rel_month) if rel_month else 0  # clientes servidos (zonas/circuito elegidos)
    exig = sm(cob_s, "exigible")
    # Collection Efficiency = Σ cobros / Σ exigible de los meses CON exigible (> 0): misma regla que energrid.vw_kpi_resumen.
    # Los cobros de un mes sin exigible (p. ej. enero, cuyas facturas vencen en febrero) no entran al numerador.
    cob_ce = sum((x["cobros"] or 0) for x in cob_s if (x["exigible"] or 0) > 0)
    kpi = {
        "perdida_pct": sm(lm_s, "perdida_kwh") / inj if inj else None,
        "ce": cob_ce / exig if exig else None,
        "m60_usd": c.get("m60"), "m60_pct": c.get("pct_m60"), "cartera_total": c.get("total"), "corte": c.get("corte"),
        "dso": c.get("dso"),
        "saidi": cm_ / cs_sys if cs_sys else None,
        "saifi": sm(rel_s, "ci") / cs_sys if cs_sys else None,
        "mttr": sm(rel_s, "minutos") / sm(rel_s, "eventos") if sm(rel_s, "eventos") else None,
        "eventos": sm(rel_s, "eventos"),
        "ami": sm(ami_s, "validas") / sm(ami_s, "esperadas") if sm(ami_s, "esperadas") else None,
        "evitable_usd": sm(lm_s, "evitable_usd"),
        "injected": inj, "billed": sm(lm_s, "billed_kwh"), "tecnica": sm(lm_s, "tecnica"),
        "no_tecnica": sm(lm_s, "no_tecnica"), "brecha": sm(lm_s, "brecha"),
        # Consumo estimado. `estimadas` es una PROPORCIÓN (0-1) de facturas, ponderada por facturas emitidas (Σ estimadas / Σ emitidas),
        # no un promedio de porcentajes mensuales. `est_n` / `emit_n` son las CANTIDADES (numerador y denominador) de ese mismo período y filtros.
        "estimadas": sm(est_s, "est_n") / sm(est_s, "emit_n") if sm(est_s, "emit_n") else None,
        "est_n": sm(est_s, "est_n"), "emit_n": sm(est_s, "emit_n"),
    }

    # Referencias del frontend: metas fijas de TARGETS + la del consumo estimado, DERIVADA de cfg_parametros (no existe una meta oficial):
    # 1 - alerta_ami_min = indisponibilidad de telemetría tolerada por la política AMI (con 93 % -> 7 %). Se informa su fuente.
    targets = dict(TARGETS)
    ami_min_cfg = (params[0] or {}).get("alerta_ami_min") if params else None
    if ami_min_cfg is not None:
        targets["est_meta"] = round(1 - float(ami_min_cfg), 4)
        targets["est_meta_fuente"] = "1 − cfg_parametros.alerta_ami_min"

    # Indicadores a los que el filtro activo NO aplica (el frontend los muestra como nota discreta; no se prorratea nada).
    na = {}
    if hasT or hasK:
        w = " y ".join((["tarifa"] if hasT else []) + (["tipo de cliente"] if hasK else []))
        na["loss"] = (f"Pérdidas de energía: el filtro por {w} no aplica (la energía inyectada no se mide por tarifa ni tipo de cliente; "
                      "no se prorratea). Se muestran con región, zona, circuito y mes.")
        na["rel"] = (f"Confiabilidad (SAIDI, SAIFI, MTTR, causas, circuitos y alertas de calidad): el filtro por {w} no aplica; "
                     "las interrupciones se miden por zona y circuito.")
        na["top10"] = f"Ranking de transformadores: no se filtra por {w}."
    if dimc:
        na["zona"] = "Criticidad y SAIDI por zona se calculan sobre la zona completa; no se recalculan con circuito, tarifa ni tipo de cliente."
    if hasC:
        na["alertas"] = ("Con un circuito seleccionado solo se muestran alertas a nivel de circuito (pérdida anómala); "
                         "las alertas de SAIDI y AMI se calculan por zona.")
    if ids or dimc:
        na["ctrl"] = "La matriz de control valida el sistema completo; no se filtra por región, zona, circuito, tarifa ni tipo de cliente."

    return dict(period_filter={"month": month, "partial": is_partial},
                filters={"region": f.region, "zones": list(ids), "circuit": circ, "tariff": tar, "client_type": ctp},
                na=na,
                partial_notice=("Mes elegido: KPI, rankings, cartera, alertas, causas, circuitos, controles y Top 10 muestran solo ese mes · "
                                "las series mensuales muestran todo el período con el mes resaltado." if is_partial else ""),
                kpi=kpi, targets=targets, params=(params[0] if params else {}), top10_loading=top10_loading,
                loss_month=loss_month, loss_zone=r["loss_zone"], ami=ami, ami_zone=r["ami_zone"], est=est, cob=cob, aging=r["aging"],
                m60_zone=r["m60_zone"], seg=r["seg"], cart_tarifa=r.get("cart_tarifa"), rel_month=rel_month,
                causes=r["causes"], recurr=r["recurr"],
                crit=r["crit"], top10=top10, al_sum=r["al_sum"], al_svc=r["al_svc"], ctrl=ctrl_rows)


# ---------------------------------------------------------------- Detalle de transformador y de cliente (modal del dashboard)
# Solo lectura y parametrizado: el id llega como entero validado por FastAPI (rango) y existencia en la dimensión; el mes se valida
# con _month_bounds (AAAA-MM). Nada se concatena en el SQL salvo constantes del código (S, TOP N, nombres de tabla de T()).
# Los endpoints NO tocan los filtros globales: el modal los llama con el mes vigente y nada más.
CLIENTS_PAGE = 20      # clientes por página (fijo)
MAX_PAGE = 5000        # tope de página (evita OFFSET absurdos)
DETAIL_ROWS = 36       # tope de lecturas / facturas por cliente (12 meses x margen para duplicados)
PAY_ROWS = 50          # tope de pagos por cliente


def _detail_period(month):
    """(bounds, vals) del mes AAAA-MM; None = todo el período. Lanza ValueError si el formato es inválido."""
    bounds = _month_bounds(month)
    vals: dict = {}
    if bounds:
        vals.update(a=bounds[0], b=bounds[1], y=bounds[0].year, m=bounds[0].month)
    return bounds, vals


def _cut_expr(bounds) -> str:
    """Último corte de cartera del mes (o de todo el período), igual que el dashboard."""
    if bounds is None:
        return f"(SELECT MAX(snapshot_date) FROM {T('vw_dso_zona_mes')})"
    return f"(SELECT MAX(snapshot_date) FROM {T('vw_dso_zona_mes')} WHERE snapshot_date >= :a AND snapshot_date < :b)"


def _transformer_header(tid: int):
    return q(f"""SELECT t.transformer_id, t.transformer_name, t.capacity_kva, t.circuit_id, c.circuit_name, t.zone_id, z.zone_name, z.region
        FROM [{S}].[dim_transformador] t LEFT JOIN [{S}].[dim_zona] z ON z.zone_id = t.zone_id
        LEFT JOIN [{S}].[dim_circuito] c ON c.circuit_id = t.circuit_id
        WHERE t.transformer_id = :id AND t.transformer_id > 0""", {"id": tid}, ttl=CATALOG_TTL)


@app.get("/api/transformer/{tid}")
def transformer_detail(tid: int = PathParam(..., ge=1, le=10_000_000), month: Optional[str] = Query(None, max_length=7)):
    """Resumen del transformador para el mes (o todo el período): pérdida, USD evitable, morosidad al último corte y SAIDI/SAIFI.
    Mismas fórmulas que el Top 10 (vw_perdida_transformador_mes, fact_cartera_snapshot y vw_interrupciones_detalle / vw_clientes_servidos)."""
    try:
        bounds, pv = _detail_period(month)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    try:
        hdr = _transformer_header(tid)
        if not hdr:
            return JSONResponse({"error": "Transformador inexistente"}, status_code=404)
        vals = {"id": tid, **pv}
        ym = " AND period_year = :y AND period_month = :m" if bounds else ""
        ps = " AND period_start >= :a AND period_start < :b" if bounds else ""
        qs = dict(
            loss=f"""SELECT SUM(injected_kwh) injected_kwh, SUM(billed_kwh) billed_kwh, SUM(perdida_total_kwh) perdida_kwh,
                SUM(perdida_tecnica_kwh) tecnica, SUM(perdida_no_tecnica_kwh) no_tecnica, SUM(brecha_medicion_kwh) brecha,
                SUM(perdida_total_kwh) * 1.0 / NULLIF(SUM(injected_kwh), 0) perdida_pct, SUM(perdida_evitable_usd) evitable_usd,
                COUNT(*) meses FROM {T('vw_perdida_transformador_mes')} WHERE transformer_id = :id{ym}""",
            cart=f"""SELECT SUM(s.balance) saldo_total, SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) saldo_mayor_60,
                SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) * 1.0 / NULLIF(SUM(s.balance), 0) pct_m60,
                {_cut_expr(bounds)} corte
                FROM [{S}].[fact_cartera_snapshot] s JOIN [{S}].[dim_cliente] c ON c.client_id = s.client_id
                WHERE c.transformer_id = :id AND s.snapshot_date = {_cut_expr(bounds)}""",
            rel=f"""WITH ev AS (SELECT COUNT(*) eventos, SUM(cliente_minutos) cm, SUM(customers_affected) ci, SUM(duration_minutes) minutos
                    FROM {T('vw_interrupciones_detalle')} WHERE transformer_id = :id{ps}),
                sv AS (SELECT SUM(clientes_servidos) cs FROM {T('vw_clientes_servidos')} WHERE transformer_id = :id)
                SELECT ev.eventos, ev.cm, ev.ci, ev.minutos, sv.cs clientes_servidos, ev.cm * 1.0 / NULLIF(sv.cs, 0) saidi,
                    ev.ci * 1.0 / NULLIF(sv.cs, 0) saifi, ev.minutos * 1.0 / NULLIF(ev.eventos, 0) mttr
                FROM ev CROSS JOIN sv""",
        )
        r = run_parallel(qs, vals)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    loss, cart, rel = (r["loss"] or [{}])[0], (r["cart"] or [{}])[0], (r["rel"] or [{}])[0]
    if not loss.get("meses"):
        loss = {}                                   # sin filas de pérdida en ese mes: se muestra "sin datos", no ceros
    return dict(period={"month": month or None, "partial": bounds is not None}, transformer=hdr[0], loss=loss,
                cartera=cart if cart.get("corte") is not None else {}, rel=rel)


@app.get("/api/transformer/{tid}/clients")
def transformer_clients(tid: int = PathParam(..., ge=1, le=10_000_000), month: Optional[str] = Query(None, max_length=7),
                        page: int = Query(1, ge=1, le=MAX_PAGE)):
    """Clientes del transformador (20 por página, mayor saldo >60 días primero) con consumo del período, saldo y tarifa.
    Los totales (n_clientes, kWh, saldo, >60 d) cubren TODOS los clientes del transformador, no solo la página."""
    try:
        bounds, pv = _detail_period(month)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    try:
        if not _transformer_header(tid):
            return JSONResponse({"error": "Transformador inexistente"}, status_code=404)
        lec = " AND l.period_year = :y AND l.period_month = :m" if bounds else ""
        sql = f"""WITH cut AS (SELECT {_cut_expr(bounds)} d),
            cl AS (SELECT client_id, client_code, client_type, tariff_id FROM [{S}].[dim_cliente] WHERE transformer_id = :id AND client_id > 0),
            con AS (SELECT l.client_id, SUM(l.consumption_kwh) kwh,
                      SUM(CASE WHEN l.reading_status = N'Estimado' THEN 1 ELSE 0 END) est,
                      SUM(CASE WHEN l.reading_status = N'Faltante' THEN 1 ELSE 0 END) falt
                    FROM [{S}].[fact_lectura_energia] l JOIN cl ON cl.client_id = l.client_id WHERE 1 = 1{lec} GROUP BY l.client_id),
            car AS (SELECT s.client_id, SUM(s.balance) saldo, SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) m60
                    FROM [{S}].[fact_cartera_snapshot] s JOIN cl ON cl.client_id = s.client_id
                    WHERE s.snapshot_date = (SELECT d FROM cut) GROUP BY s.client_id)
            SELECT cl.client_id, cl.client_code, cl.client_type, COALESCE(t.tariff_name, N'Desconocida') tarifa,
                ISNULL(con.kwh, 0) kwh, ISNULL(con.est, 0) est, ISNULL(con.falt, 0) falt, ISNULL(car.saldo, 0) saldo, ISNULL(car.m60, 0) m60,
                COUNT(*) OVER () n_clientes, SUM(ISNULL(con.kwh, 0)) OVER () kwh_total,
                SUM(ISNULL(car.saldo, 0)) OVER () saldo_total, SUM(ISNULL(car.m60, 0)) OVER () m60_total
            FROM cl LEFT JOIN con ON con.client_id = cl.client_id LEFT JOIN car ON car.client_id = cl.client_id
                LEFT JOIN [{S}].[dim_tarifa] t ON t.tariff_id = cl.tariff_id
            ORDER BY ISNULL(car.m60, 0) DESC, cl.client_id OFFSET :off ROWS FETCH NEXT :n ROWS ONLY"""
        rows = q(sql, {"id": tid, **pv, "off": (page - 1) * CLIENTS_PAGE, "n": CLIENTS_PAGE})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    first = rows[0] if rows else {}
    totals = {k: first.get(k) for k in ("n_clientes", "kwh_total", "saldo_total", "m60_total")} if rows else {}
    for x in rows:
        for k in totals:
            x.pop(k, None)
    return dict(period={"month": month or None, "partial": bounds is not None}, page=page, size=CLIENTS_PAGE, totals=totals, rows=rows)


@app.get("/api/client/{cid}")
def client_detail(cid: int = PathParam(..., ge=1, le=100_000_000), month: Optional[str] = Query(None, max_length=7)):
    """Detalle de un cliente en el período: lecturas (Leído/Estimado/Faltante), facturas con lo pagado y pagos recibidos.
    Cada lista tiene tope (DETAIL_ROWS / PAY_ROWS); `truncated` indica si hay más filas de las mostradas."""
    try:
        bounds, pv = _detail_period(month)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    try:
        hdr = q(f"""SELECT c.client_id, c.client_code, c.client_type, COALESCE(t.tariff_name, N'Desconocida') tarifa, c.zone_id, z.zone_name,
            c.circuit_id, ci.circuit_name, c.transformer_id, tr.transformer_name, c.active
            FROM [{S}].[dim_cliente] c LEFT JOIN [{S}].[dim_tarifa] t ON t.tariff_id = c.tariff_id
            LEFT JOIN [{S}].[dim_zona] z ON z.zone_id = c.zone_id LEFT JOIN [{S}].[dim_circuito] ci ON ci.circuit_id = c.circuit_id
            LEFT JOIN [{S}].[dim_transformador] tr ON tr.transformer_id = c.transformer_id
            WHERE c.client_id = :id AND c.client_id > 0""", {"id": cid}, ttl=CATALOG_TTL)
        if not hdr:
            return JSONResponse({"error": "Cliente inexistente"}, status_code=404)
        vals = {"id": cid, **pv}
        ym = " AND {a}.period_year = :y AND {a}.period_month = :m" if bounds else ""
        pp = " AND payment_date >= :a AND payment_date < :b" if bounds else ""
        qs = dict(
            lecturas=f"""SELECT TOP {DETAIL_ROWS + 1} l.reading_date, l.period_year, l.period_month, l.reading_status, l.registered_kwh, l.estimated_kwh,
                l.consumption_kwh, l.ami_valid, m.meter_serial
                FROM [{S}].[fact_lectura_energia] l LEFT JOIN [{S}].[dim_medidor] m ON m.meter_id = l.meter_id
                WHERE l.client_id = :id{ym.format(a='l')} ORDER BY l.reading_date DESC, l.reading_id DESC""",
            facturas=f"""SELECT TOP {DETAIL_ROWS + 1} f.invoice_id, f.invoice_date, f.due_date, f.period_year, f.period_month, f.billed_kwh, f.is_estimated,
                f.amount_due, ISNULL(p.pagado, 0) pagado, f.amount_due - ISNULL(p.pagado, 0) saldo
                FROM [{S}].[fact_facturacion] f OUTER APPLY (SELECT SUM(pg.payment_amount) pagado FROM [{S}].[fact_pago] pg WHERE pg.invoice_id = f.invoice_id) p
                WHERE f.client_id = :id{ym.format(a='f')} ORDER BY f.invoice_date DESC, f.invoice_id DESC""",
            pagos=f"""SELECT TOP {PAY_ROWS + 1} payment_id, invoice_id, payment_date, payment_amount, payment_method, is_date_imputed
                FROM [{S}].[fact_pago] WHERE client_id = :id{pp}
                ORDER BY payment_date DESC, payment_id DESC""",
        )
        r = run_parallel(qs, vals)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    cut = lambda rows, n: (rows[:n], len(rows) > n)
    lec, lt = cut(r["lecturas"], DETAIL_ROWS)
    fac, ft = cut(r["facturas"], DETAIL_ROWS)
    pag, pt_ = cut(r["pagos"], PAY_ROWS)
    return dict(period={"month": month or None, "partial": bounds is not None}, client=hdr[0], lecturas=lec, facturas=fac, pagos=pag,
                truncated={"lecturas": lt, "facturas": ft, "pagos": pt_}, limits={"lecturas": DETAIL_ROWS, "facturas": DETAIL_ROWS, "pagos": PAY_ROWS})


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
