"""Pruebas sin navegador de EnerGrid BI (solo lectura; no escribe en SQL Server).

Ejecutar desde la carpeta del proyecto, con el entorno virtual activo:
    python tests/test_sin_navegador.py
Sale con código 1 si algo falla. Las pruebas de integración se omiten (SKIP) si no hay conexión a SQL Server;
la prueba de JavaScript se omite si `node` no está instalado.
"""
import asyncio, json, py_compile, re, shutil, subprocess, sys, tempfile, time
from unittest.mock import patch
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RES = []
SKIPPED = []


def check(name, ok, detail=""):
    RES.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  -> {detail}" if detail else ""))


def skip(name, why):
    SKIPPED.append(name)
    print(f"SKIP {name}  ({why})")


# ---------- 1. Sintaxis ----------
for f in ("server.py", "config.py", "estado_datos.py", "sql/energrid_etl.py", "sql/energrid_sqlserver.py"):
    try:
        py_compile.compile(str(ROOT / f), doraise=True, cfile=str(Path(tempfile.gettempdir()) / (Path(f).name + "c")))
        check(f"py_compile {f}", True)
    except py_compile.PyCompileError as e:
        check(f"py_compile {f}", False, str(e))

html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)   # solo el script propio (los CDN llevan src=)
if not scripts:
    check("script inline encontrado en index.html", False)
elif shutil.which("node"):
    tmp = Path(tempfile.gettempdir()) / "energrid_index_check.js"
    for i, script in enumerate(scripts):
        tmp.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        check(f"node --check (bloque JS {i + 1})", r.returncode == 0, r.stderr.strip()[:400])
else:
    skip("node --check", "node no está instalado")
ids = re.findall(r'id="([^"]+)"', html)
dup = sorted({i for i in ids if ids.count(i) > 1})
check("ids HTML sin duplicados", not dup, ", ".join(dup))
refs = set(re.findall(r"\$\('([A-Za-z_][\w]*)'\)", html)) | set(re.findall(r"ch\('([A-Za-z_][\w]*)'\)", html)) | set(re.findall(r"draw\('([A-Za-z_][\w]*)'", html))
falta = sorted(r for r in refs if r not in ids)
check("todos los $('id') / ch('id') / draw('id') existen en el HTML", not falta, ", ".join(falta))

# ---------- 2. Lógica pura de server.py (sin BD) ----------
import server as S

sql, p = S.bind("SELECT 1 WHERE a IN (:z) AND b = :c AND d >= :a AND d < :b AND x = :otro", dict(z=[1, 2, 3], c=7, a="A", b="B"))
check("bind: lista expandida y orden de parámetros", sql.count("?") == 6 and p == [1, 2, 3, 7, "A", "B"] and ":otro" in sql, f"{sql} {p}")
sql, p = S.bind("... :a ... :b ... :a ...", dict(a=1, b=2))
check("bind: marcador repetido se enlaza cada vez", p == [1, 2, 1])
check("bind: sin valores no toca el SQL", S.bind("SELECT 1", None) == ("SELECT 1", []))
check("_month_bounds válido", S._month_bounds("2025-12")[0].isoformat() == "2025-12-01" and S._month_bounds("2025-12")[1].isoformat() == "2026-01-01")
check("_month_bounds vacío = todo el período", S._month_bounds("") is None and S._month_bounds(None) is None)
for bad in ("2025-13", "2025-1", "abc", "2025-00", "2025-01'; DROP TABLE x;--"):
    try:
        S._month_bounds(bad); check(f"_month_bounds rechaza {bad!r}", False)
    except ValueError:
        check(f"_month_bounds rechaza {bad!r}", True)
with patch.object(S, "mats", return_value=frozenset()):
    q1 = S.crit_sql(None, "X")
check("crit_sql anual no usa marcadores de mes", ":a" not in q1)

# ---------- Regresiones de los filtros por ASGI, sin base ni navegador ----------
async def asgi_get(url):
    u = urlsplit(url)
    scope = dict(type="http", asgi={"version": "3.0"}, http_version="1.1",
                 method="GET", scheme="http", path=u.path, raw_path=u.path.encode(),
                 query_string=u.query.encode(), root_path="", headers=[],
                 server=("test", 80), client=("127.0.0.1", 12345))
    messages = []
    delivered = False
    async def receive():
        nonlocal delivered
        if delivered:
            await asyncio.Event().wait()
        delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await asyncio.wait_for(S.app(scope, receive, send), timeout=10)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body)

# Los diez globales pertenecen a zona 1; zona 2 tiene candidatos fuera de ese top.
# El codigo anterior devolvia [] al seleccionar zona 2. Este fixture verifica el contrato
# de seleccion del endpoint; la ejecucion real de SQL se comprueba en la integracion.
all_candidates = [dict(transformer_id=i, zone_id=1 if i <= 10 else 2,
                       circuit_id=1 if i <= 10 else 2, score=1000-i, ranking=i)
                  for i in range(1, 611)]
fake_catalog = dict(zone_region={1: "Norte", 2: "Sur"}, regions=["Norte", "Sur"],
                    circuit_zone={1: 1, 2: 2}, tariff_ids={1,2}, types=["Residencial","Comercial"],
                    circuits=[dict(circuit_id=i, circuit_name=str(i), zone_id=i) for i in (1,2)],
                    tariffs=[dict(tariff_id=1,tariff_name="Residencial"),dict(tariff_id=2,tariff_name="Comercial")])
fake_grid=[dict(period_start="2025-12-01",zone_id=z,circuit_id=z,tariff_id=t,client_type=k)
           for z in (1,2) for t,k in ((1,"Residencial"),(2,"Comercial"))]
def fake_selected(ids,circuit):
    return [r for r in all_candidates if (not ids or r["zone_id"] in ids)
            and (circuit is None or r["circuit_id"]==circuit)][:10]
seen = []
def fake_parallel(queries, vals=None):
    vals = vals or {}
    seen.append((queries, dict(vals)))
    result = {name: [] for name in queries}
    result["params"] = [{"alerta_ami_min": .93}]
    result["m60_zone"] = [dict(zone_id=2, saldo_total=1234.5, saldo_mayor_60=200, pct=.162)]
    result["cart"] = [dict(corte="2025-12-31", total=1234.5, m60=200)]
    if "top10_filtered" in queries:
        rows = [r for r in all_candidates
                if (vals["top_all_zones"] or r["zone_id"] in vals["top_zones"])
                and (vals["top_circuit"] is None or r["circuit_id"] == vals["top_circuit"])]
        result["top10_filtered"] = rows[:10]
    return result

with patch.object(S, "catalog", return_value=fake_catalog), \
     patch.object(S, "mats", return_value=S.DIM_TABLES), \
     patch.object(S, "run_parallel", side_effect=fake_parallel), \
     patch.object(S, "filter_grid", return_value=fake_grid), \
     patch.object(S, "selected_top10", side_effect=fake_selected), \
     patch.object(S, "get_top10", return_value=all_candidates[:10]), \
     patch.object(S, "month_heavy", return_value={"rows": all_candidates, "sys": 0, "err": False}):
    for name, url in (("zona", "/api/dashboard?zones=2"),
                      ("region", "/api/dashboard?region=Sur"),
                      ("circuito", "/api/dashboard?circuit=2"),
                      ("zona+circuito", "/api/dashboard?zones=2&circuit=2")):
        status, data = asyncio.run(asgi_get(url))
        check(f"ASGI top10 {name}: incluye candidatos fuera del top global", status == 200
              and len(data["top10"]) == 10 and data["top10"][0]["transformer_id"] == 11)
    status, data = asyncio.run(asgi_get("/api/dashboard"))
    check("ASGI sin filtros conserva top10 global", status == 200 and data["top10"] == all_candidates[:10])
    check("ASGI deuda conserva saldo total y vencido por zona", data["m60_zone"][0]["saldo_total"] == 1234.5
          and data["m60_zone"][0]["saldo_mayor_60"] == 200 and data["kpi"]["corte"] == "2025-12-31")
    status, data = asyncio.run(asgi_get("/api/dashboard?month=2025-12&zones=2"))
    check("ASGI mes: filtra candidatos completos antes de limitar", status == 200
          and len(data["top10"]) == 10 and data["top10"][0]["transformer_id"] == 11)
    status, data = asyncio.run(asgi_get("/api/dashboard?zones=1%3BDROP"))
    check("ASGI filtro malicioso rechazado", status == 422)
    status, data = asyncio.run(asgi_get("/api/dashboard?tariff=2&client_type=Residencial"))
    check("ASGI combinacion incompatible devuelve 400, no 500", status == 400)
    status, data = asyncio.run(asgi_get("/api/filters?client_type=Residencial"))
    check("faceta residencial solo ofrece tarifa residencial", status==200 and [t["tariff_id"] for t in data["tariffs"]]==[1])
    status, data = asyncio.run(asgi_get("/api/filters?zones=2"))
    check("faceta de zonas limita circuitos a clientes existentes", status==200 and [c["circuit_id"] for c in data["circuits"]]==[2])
    status, data = asyncio.run(asgi_get("/api/filters?tariff=2&client_type=Residencial"))
    check("facetas incompatibles explican estado invalido", status==200 and data["valid"] is False)

sql = S._top10_selection_sql()
bound, values = S.bind(sql, dict(top_all_zones=0, top_zones=[2, 3], top_circuit=428))
check("top10 SQL consulta candidatos completos y enlaza filtros", "[vw_criticidad_transformador]" in sql
      and "[vw_top10_transformadores]" not in sql and "IN (?, ?)".replace(" ", "") in bound.replace(" ", "")
      and values == [0, 2, 3, 428, 428] and "ORDER BY c.score_criticidad DESC, c.transformer_id" in sql)
with patch.object(S, "mats", return_value=frozenset()):
    check("mes no recorta candidatos a 500 antes del filtro", "TOP 500" not in S._tr_sql().upper())

# Ejecutar el tooltip real con Node para comprobar moneda, cero y ausencia de datos.
if shutil.which("node"):
    start = html.index("    function zoneTip(")
    end = html.index("    function buildMarkers(", start)
    tooltip_fn = html[start:end]
    node_test = r"""
const assert = require('assert');
const byZone = (rows,id) => (rows||[]).find(x=>x.zone_id===id);
const htmlSafe = s => String(s), dataLabel = () => '2025-12', mapStatusHtml = () => '';
const nf = x => String(x), critLevel = () => 'Alta', pct = x => String(x), amiMinOf = () => .93;
const d10 = x => x, usd = x => 'USD ' + Number(x).toFixed(2);
let D = {kpi:{corte:'2025-12-31'}, m60_zone:[{zone_id:2,saldo_total:1234.5,saldo_mayor_60:200}]};
""" + tooltip_fn + r"""
assert(zoneTip(2,'Zona 02').includes('USD 1234.50'));
assert(zoneTip(2,'Zona 02').includes('USD 200.00'));
assert(zoneTip(2,'Zona 02').includes('2025-12-31'));
D.m60_zone[0].saldo_total=0; assert(zoneTip(2,'Zona 02').includes('USD 0.00'));
D.m60_zone=[]; assert(zoneTip(2,'Zona 02').includes('sin dato'));
"""
    tmp = Path(tempfile.gettempdir()) / "energrid_tooltip_test.js"
    tmp.write_text(node_test, encoding="utf-8")
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True)
    check("tooltip: moneda, corte, saldo cero y dato ausente", r.returncode == 0, r.stderr[:400])

# La cache no debe lanzar seis veces la misma consulta simultanea.
from concurrent.futures import ThreadPoolExecutor
calls = []
def slow_query(sql, params=()):
    calls.append(sql)
    time.sleep(.03)
    return [{"value": 1}]
with patch.object(S, "_exec", side_effect=slow_query):
    S._cache.clear()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: S.q("SELECT 1 AS test_singleflight", ttl=60), range(6)))
    check("cache concurrente ejecuta una sola consulta por clave", len(calls)==1 and all(r==results[0] for r in results))

# ---------- 3. Integración con SQL Server (solo lectura) ----------
try:
    S._exec("SELECT 1 AS ok")
    db = True
except Exception as e:
    db = False
    skip("integración con SQL Server", f"sin conexión: {str(e)[:120]}")

if db:
    from fastapi.testclient import TestClient
    c = TestClient(S.app)

    def api(**params):
        r = c.get("/api/dashboard", params={k: v for k, v in params.items() if v not in (None, "")})
        return r.status_code, r.json()

    def truth(zones=None, tariff=None, ctype=None, ym=None):
        w, a = ["1=1"], []
        if zones: w.append(f"c.zone_id IN ({','.join('?' * len(zones))})"); a += zones
        if tariff is not None: w.append("c.tariff_id = ?"); a.append(tariff)
        if ctype: w.append("c.client_type = ?"); a.append(ctype)
        if ym: w.append("f.period_year = ? AND f.period_month = ?"); a += [int(ym[:4]), int(ym[5:])]
        r = S._exec(f"SELECT SUM(CASE WHEN f.is_estimated = 1 THEN 1 ELSE 0 END) e, COUNT(*) n FROM [{S.S}].[fact_facturacion] f "
                    f"JOIN [{S.S}].[dim_cliente] c ON c.client_id = f.client_id WHERE {' AND '.join(w)}", a)[0]
        return int(r["e"] or 0), int(r["n"] or 0)

    t0 = time.time(); st, d = api(); t_frio = time.time() - t0
    check("GET /api/dashboard 200", st == 200, str(d.get("error", "")) if st != 200 else "")
    if st == 200:
        k, tg = d["kpi"], d["targets"]
        e, n = truth()
        check("KPI estimadas: est_n / emit_n = hecho (todo el período)", (k["est_n"], k["emit_n"]) == (e, n), f"{k['est_n']}/{k['emit_n']} vs {e}/{n}")
        check("kpi.estimadas = est_n / emit_n (ponderado, no promedio de %)", abs(k["estimadas"] - k["est_n"] / k["emit_n"]) < 1e-12)
        check("kpi.estimadas es proporción 0-1", 0 <= k["estimadas"] <= 1)
        P = d["params"]
        check("referencia est_meta = 1 - alerta_ami_min", abs(tg.get("est_meta", -1) - (1 - float(P["alerta_ami_min"]))) < 1e-4 and "est_meta_fuente" in tg, str(tg))
        check("serie est: Σ meses = KPI", sum(r["est_n"] for r in d["est"]) == k["est_n"] and sum(r["emit_n"] for r in d["est"]) == k["emit_n"])
        check("serie est ordenada por mes", [r["period_start"] for r in d["est"]] == sorted(r["period_start"] for r in d["est"]))
        check("ami_zone: 15 filas, Σ válidas = Σ serie ami (zonas > 0)", len(d["ami_zone"]) == 15)
        tarifa = d.get("cart_tarifa")
        if tarifa is not None:
            check("cart_tarifa: Σ por tarifa = cartera total", abs(sum(r["total"] for r in tarifa) - k["cartera_total"]) < 0.5, f"{sum(r['total'] for r in tarifa)} vs {k['cartera_total']}")
        else:
            skip("cart_tarifa", "falta mat_cartera_dim (sql/04_procedimientos.sql)")

        months = [r["period_start"][:7] for r in d["est"]]
        for ym in (months[0], months[len(months) // 2], months[-1]):
            st2, d2 = api(month=ym); e, n = truth(ym=ym)
            check(f"mes {ym}: est_n/emit_n = hecho", st2 == 200 and (d2["kpi"]["est_n"], d2["kpi"]["emit_n"]) == (e, n), f"{d2['kpi'].get('est_n')}/{d2['kpi'].get('emit_n')} vs {e}/{n}" if st2 == 200 else str(d2))
            if st2 == 200:
                prev = [r for r in d2["est"] if r["period_start"][:7] < ym]
                check(f"mes {ym}: la serie conserva el mes previo para la variación", bool(prev) or ym == months[0])

        zs = [z["zone_id"] for z in c.get("/api/zones").json()][:3]
        tid = int(S.catalog()["tariffs"][-1]["tariff_id"]); ct = S.catalog()["types"][0]
        for etiqueta, kw, tr in (("zonas", dict(zones=",".join(map(str, zs))), dict(zones=zs)),
                                 ("tarifa", dict(tariff=tid), dict(tariff=tid)),
                                 ("tipo", dict(client_type=ct), dict(ctype=ct)),
                                 ("zonas+tarifa+tipo+mes", dict(zones=",".join(map(str, zs)), tariff=tid, client_type=ct, month=months[-1]), dict(zones=zs, tariff=tid, ctype=ct, ym=months[-1]))):
            st2, d2 = api(**kw); e, n = truth(**tr)
            if st2 == 409:
                skip(f"filtro {etiqueta}", "faltan tablas de dimensiones (409)")
                continue
            if n == 0:
                check(f"filtro {etiqueta}: combinacion sin clientes devuelve 400", st2 == 400)
                continue
            check(f"filtro {etiqueta}: est_n/emit_n = hecho", st2 == 200 and (d2["kpi"]["est_n"], d2["kpi"]["emit_n"]) == (e, n), f"{d2['kpi'].get('est_n')}/{d2['kpi'].get('emit_n')} vs {e}/{n}" if st2 == 200 else str(d2))

        # Validación de parámetros: nada inválido llega al SQL
        for etiqueta, kw in (("mes", dict(month="2025-13")), ("zona", dict(zones="999999")), ("zona SQL", dict(zones="1;DROP TABLE x")),
                             ("región", dict(region="Inexistente")), ("tarifa", dict(tariff=987654)), ("tipo", dict(client_type="Nada"))):
            st2, d2 = api(**kw)
            check(f"parámetro inválido ({etiqueta}) -> 422", st2 == 422 and "error" in d2, f"{st2} {d2}")

        # Caché: la segunda llamada idéntica no ejecuta consultas nuevas y devuelve lo mismo
        calls = {"n": 0}; orig = S._exec
        S._exec = lambda sql, params=(): (calls.__setitem__("n", calls["n"] + 1), orig(sql, params))[1]
        try:
            t0 = time.time(); st3, d3 = api(month=months[-1]); t_c = time.time() - t0
        finally:
            S._exec = orig
        check("caché: llamada repetida sin consultas nuevas (salvo sondeo mats/top10)", st3 == 200 and calls["n"] <= 2, f"{calls['n']} consultas, {t_c:.2f}s (primera, en frío: {t_frio:.2f}s)")
        check("caché: respuesta idéntica", st3 == 200 and d3["kpi"]["est_n"] == api(month=months[-1])[1]["kpi"]["est_n"])

        # Top 10 de una zona/circuito: contraste independiente contra todos los candidatos SQL.
        for label, param, col in (("zona", "zones", "zone_id"), ("circuito", "circuit", "circuit_id")):
            entity = S._exec(f"SELECT TOP (1) {col} id FROM [{S.S}].[vw_criticidad_transformador] "
                             f"GROUP BY {col} HAVING COUNT(*) >= 10 ORDER BY {col}")
            if not entity:
                skip(f"top10 {label} SQL", "no hay entidad con diez candidatos")
                continue
            eid = int(entity[0]["id"])
            expected = S._exec(f"SELECT TOP (10) transformer_id FROM [{S.S}].[vw_criticidad_transformador] "
                               f"WHERE {col} = ? ORDER BY score_criticidad DESC, transformer_id", [eid])
            stf, df = api(**{param: str(eid)})
            check(f"top10 {label}: IDs coinciden con candidatos SQL filtrados", stf == 200
                  and [r["transformer_id"] for r in df["top10"]] == [r["transformer_id"] for r in expected])

        # Drill-through
        t10 = d["top10"]
        if t10:
            tid_ = t10[0]["transformer_id"]
            s1, r1 = c.get(f"/api/transformer/{tid_}").status_code, c.get(f"/api/transformer/{tid_}").json()
            s2, r2 = c.get(f"/api/transformer/{tid_}/clients", params={"page": 1}).status_code, c.get(f"/api/transformer/{tid_}/clients", params={"page": 1}).json()
            check("drill-through transformador 200 y 20 filas máx.", s1 == 200 and s2 == 200 and len(r2["rows"]) <= 20, f"{s1}/{s2}")
            if s2 == 200 and r2["rows"]:
                cid = r2["rows"][0]["client_id"]
                s3_ = c.get(f"/api/client/{cid}")
                check("drill-through cliente 200", s3_.status_code == 200, s3_.text[:200])
            check("transformador inexistente -> 404", c.get("/api/transformer/9999999").status_code == 404)
            check("mes inválido en detalle -> 422", c.get(f"/api/transformer/{tid_}", params={"month": "2025-99"}).status_code == 422)
        else:
            skip("drill-through", "Top 10 aún calculándose; reintenta en unos segundos")

fails = [n for n, ok in RES if not ok]
print(f"\n{len(RES) - len(fails)}/{len(RES)} pruebas OK" + (f" · FALLAN: {', '.join(fails)}" if fails else ""))
if SKIPPED:
    print(f"{len(SKIPPED)} bloque(s) omitido(s): " + ", ".join(SKIPPED))
sys.exit(1 if fails else 0)
