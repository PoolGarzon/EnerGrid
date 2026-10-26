"""EnerGrid - ETL: Extraccion, transformacion, limpieza y carga a SQL Server.

Uso:
    python sql/energrid_etl.py --server "localhost\SQLEXPRESS" --database EnerGrid
    python sql/energrid_etl.py --no-load
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from energrid_config import (CLIENT_TYPES, DUE_DAYS, FIXED_CHARGE, INVOICE_DAY, PAY_METHODS, REGIONS,
                             STATUSES, TARIFF_BY_CLIENT_TYPE)


# =====================================================================
# Especificación por tabla
# =====================================================================
def _per_date(d, day):
    """Fecha derivada del periodo (año, mes) y de un día fijo del calendario."""
    return pd.to_datetime(dict(year=d["period_year"], month=d["period_month"], day=day), errors="coerce")


def _from_key(d, hours=0):
    """Fecha derivada de la llave de tiempo yyyymmdd (columna redundante e íntegra)."""
    return pd.to_datetime(d["date_key"].astype(str), format="%Y%m%d", errors="coerce") + pd.Timedelta(hours=hours)


# pk: llave primaria | ids: enteros | fks: (columna, tabla_padre) | dates: fechas | ts: timestamp
# date_fix: reglas para reparar fechas inválidas | date_null: fechas que admiten nulo
# nums: numéricas >= 0 | nullable: nums que admiten nulo | group: agrupación para outliers (> 8x mediana)
# cats: categorías válidas | biz: llave de negocio única | unk: FKs que pueden quedar como 'Desconocido' (-1)
SPEC = {
    "dim_zona": dict(pk=["zone_id"], ids=["zone_id"], cats={"region": REGIONS}),
    "dim_circuito": dict(pk=["circuit_id"], ids=["circuit_id", "zone_id"], fks=[("zone_id", "dim_zona")],
                         cats={"voltage_level": ["Baja", "Media"]}, unk=["zone_id"]),
    "dim_transformador": dict(pk=["transformer_id"], ids=["transformer_id", "circuit_id", "zone_id"],
                              fks=[("circuit_id", "dim_circuito"), ("zone_id", "dim_zona")],
                              nums=["capacity_kva"], unk=["circuit_id", "zone_id"]),
    "dim_tarifa": dict(pk=["tariff_id"], ids=["tariff_id"], nums=["rate_per_kwh"]),
    "dim_causa": dict(pk=["cause_id"], ids=["cause_id"]),
    "dim_tiempo": dict(pk=["date_key"], ids=["date_key"], dates=["date"]),
    "dim_cliente": dict(pk=["client_id"],
                        ids=["client_id", "tariff_id", "zone_id", "circuit_id", "transformer_id"],
                        fks=[("tariff_id", "dim_tarifa"), ("zone_id", "dim_zona"),
                             ("circuit_id", "dim_circuito"), ("transformer_id", "dim_transformador")],
                        dates=["connection_date"], date_null=["connection_date"],
                        cats={"client_type": CLIENT_TYPES}, unk=["transformer_id"]),
    "dim_medidor": dict(pk=["meter_id"], ids=["meter_id", "client_id"], fks=[("client_id", "dim_cliente")],
                        dates=["installed_date"], date_null=["installed_date"], unk=["client_id"],
                        cats={"meter_type": ["Inteligente", "Convencional"],
                              "status": ["Activo", "Reemplazado"]}),
    "fact_lectura_energia": dict(pk=["reading_id"],
                                 ids=["reading_id", "client_id", "meter_id", "date_key",
                                      "period_year", "period_month"],
                                 fks=[("client_id", "dim_cliente"), ("meter_id", "dim_medidor")],
                                 dates=["reading_date"], date_fix={"reading_date": _from_key},
                                 cats={"reading_status": STATUSES},
                                 nums=["registered_kwh", "estimated_kwh", "consumption_kwh"],
                                 nullable=["registered_kwh", "estimated_kwh", "consumption_kwh"],
                                 group="client_id", unk=["meter_id"],
                                 biz=["client_id", "period_year", "period_month"]),
    "fact_facturacion": dict(pk=["invoice_id"],
                             ids=["invoice_id", "client_id", "reading_id", "period_year", "period_month"],
                             fks=[("client_id", "dim_cliente"), ("reading_id", "fact_lectura_energia")],
                             dates=["invoice_date", "due_date"],
                             date_fix={"invoice_date": lambda d: _per_date(d, INVOICE_DAY),
                                       "due_date": lambda d: d["invoice_date"] + pd.Timedelta(days=DUE_DAYS)},
                             nums=["billed_kwh", "amount_due"],
                             group="client_id", biz=["client_id", "period_year", "period_month"]),
    "fact_pago": dict(pk=["payment_id"], ids=["payment_id", "invoice_id", "client_id"],
                      fks=[("invoice_id", "fact_facturacion")], dates=["payment_date"], date_null=["payment_date"],
                      cats={"payment_method": PAY_METHODS}, nums=["payment_amount"]),
    "fact_energia_transformador_diaria": dict(
        pk=["energy_record_id"], ids=["energy_record_id", "date_key", "transformer_id"],
        fks=[("transformer_id", "dim_transformador")], dates=["date"], date_fix={"date": _from_key},
        nums=["injected_kwh", "delivered_kwh"], group="transformer_id", biz=["transformer_id", "date_key"]),
    "fact_interrupcion": dict(pk=["outage_id"],
                              ids=["outage_id", "date_key", "circuit_id", "transformer_id",
                                   "zone_id", "cause_id", "customers_affected"],
                              fks=[("circuit_id", "dim_circuito"), ("transformer_id", "dim_transformador"),
                                   ("zone_id", "dim_zona"), ("cause_id", "dim_causa")],
                              dates=["start_datetime", "end_datetime"], ts=["start_datetime", "end_datetime"],
                              date_fix={"start_datetime": lambda d: _from_key(d, 12),
                                        "end_datetime": lambda d: d["start_datetime"]
                                        + pd.to_timedelta(d["duration_minutes"], unit="m")},
                              unk=["transformer_id", "circuit_id", "cause_id"]),
}

# Miembro 'Desconocido' (-1) por dimensión: permite conservar el hecho cuando la dimensión es irrecuperable
UNKNOWN = {
    "dim_zona": dict(zone_id=-1, zone_name="Desconocida", region="Desconocida"),
    "dim_circuito": dict(circuit_id=-1, circuit_name="Desconocido", zone_id=-1, voltage_level="Desconocido"),
    "dim_transformador": dict(transformer_id=-1, transformer_name="Desconocido", circuit_id=-1, zone_id=-1,
                              capacity_kva=0),
    "dim_tarifa": dict(tariff_id=-1, tariff_name="Desconocida", rate_per_kwh=0.0),
    "dim_causa": dict(cause_id=-1, cause_name="Desconocida", planned=False),
    "dim_cliente": dict(client_id=-1, client_code="Desconocido", client_type="Desconocido", tariff_id=-1,
                        zone_id=-1, circuit_id=-1, transformer_id=-1, active=False),
    "dim_medidor": dict(meter_id=-1, meter_serial="Desconocido", client_id=-1, meter_type="Desconocido",
                        ami_enabled=False, status="Desconocido"),
}


# =====================================================================
# Utilidades de limpieza
# =====================================================================
def _is_text(s):
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _parse_dates(s):
    s = s.astype("string").str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        todo = out.isna() & s.notna()
        if not todo.any():
            break
        parsed = pd.to_datetime(s[todo], format=fmt, errors="coerce").astype("datetime64[ns]")
        out = out.fillna(parsed)
    return out


def _reject(df, bad, reason, log):
    """Cuarentena: saca las filas irrecuperables y las deja registradas con su motivo."""
    bad = np.asarray(bad.fillna(True) if hasattr(bad, "fillna") else bad, dtype=bool)
    if bad.any():
        r = df.loc[bad].copy()
        r["rejection_reason"] = reason
        log.append(r)
    return df.loc[~bad]


def _nearest_in_circuit(df, bad, transformers):
    """Transformador más cercano (GIS) dentro del mismo circuito para las filas `bad`. Devuelve Serie ix -> transformer_id."""
    T = transformers[transformers["transformer_id"] > 0][["transformer_id", "circuit_id", "latitude", "longitude"]].dropna()
    c = df.loc[bad, ["circuit_id", "latitude", "longitude"]].dropna().rename_axis("ix").reset_index()
    m = c.merge(T, on="circuit_id", suffixes=("", "_t"))
    m["d"] = (m["latitude"] - m["latitude_t"]) ** 2 + (m["longitude"] - m["longitude_t"]) ** 2
    return m.loc[m.groupby("ix")["d"].idxmin()].set_index("ix")["transformer_id"].astype("int64")


def _recover_fks(name, df, clean, rep):
    """Repara llaves foráneas inválidas usando información redundante e íntegra del propio dato."""
    ok = lambda c, parent: df[c].isin(set(clean[parent][c]))
    if name == "dim_cliente":                               # la tarifa se deduce del tipo de cliente
        bad = ~ok("tariff_id", "dim_tarifa")
        df.loc[bad, "tariff_id"] = df.loc[bad, "client_type"].map(TARIFF_BY_CLIENT_TYPE).astype("int64")
        rep("fk_repaired:tariff_id<-client_type", bad.sum())
        bad = ~ok("transformer_id", "dim_transformador")    # transformador <- GIS: el más cercano dentro del mismo circuito
        if bad.any():
            best = _nearest_in_circuit(df, bad, clean["dim_transformador"])
            df.loc[best.index, "transformer_id"] = best
            rep("fk_repaired:transformer_id<-nearest_in_circuit(GIS)", len(best))
    elif name == "fact_lectura_energia":
        mc = clean["dim_medidor"].set_index("meter_id")["client_id"]
        bad = ~ok("client_id", "dim_cliente")              # cliente <- medidor
        cand = df["meter_id"].map(mc)
        fix = bad & cand.notna()
        df.loc[fix, "client_id"] = cand[fix].astype("int64")
        rep("fk_repaired:client_id<-meter", fix.sum())
        bad = ~ok("meter_id", "dim_medidor")               # medidor <- cliente + fecha (vigente a esa fecha)
        if bad.any():
            dm = clean["dim_medidor"]
            cand = dm[dm["meter_id"] > 0].dropna(subset=["installed_date"]).sort_values("installed_date")
            cand = cand[["client_id", "meter_id", "installed_date"]].astype({"installed_date": "datetime64[ns]"})
            tgt = df.loc[bad, ["client_id", "reading_date"]].astype({"reading_date": "datetime64[ns]"})
            tgt = tgt.rename_axis("ix").reset_index().sort_values("reading_date")
            got = pd.merge_asof(tgt, cand, left_on="reading_date", right_on="installed_date", by="client_id")
            got = got.set_index("ix")["meter_id"].dropna()
            df.loc[got.index, "meter_id"] = got.astype("int64")
            rep("fk_repaired:meter_id<-client+date", len(got))
    elif name == "fact_facturacion":
        L = clean["fact_lectura_energia"]
        L = L[L["client_id"] > 0]
        bad = ~ok("client_id", "dim_cliente")              # cliente <- lectura
        cl = df["reading_id"].map(L.set_index("reading_id")["client_id"])
        fix = bad & cl.notna()
        df.loc[fix, "client_id"] = cl[fix].astype("int64")
        rep("fk_repaired:client_id<-reading", fix.sum())
        bad = ~ok("reading_id", "fact_lectura_energia")    # lectura <- (cliente, periodo)
        if bad.any():
            biz = L.set_index(["client_id", "period_year", "period_month"])["reading_id"]
            idx = pd.MultiIndex.from_frame(df.loc[bad, ["client_id", "period_year", "period_month"]])
            got = pd.Series(biz.reindex(idx).to_numpy(), index=df.index[bad]).dropna()
            df.loc[got.index, "reading_id"] = got.astype("int64")
            rep("fk_repaired:reading_id<-client+period", len(got))
    elif name == "fact_interrupcion":                       # circuito y zona se derivan del transformador
        bad = ~df["transformer_id"].isin(set(clean["dim_transformador"]["transformer_id"]))
        if bad.any():                                       # transformador huérfano: GIS dentro del circuito informado
            best = _nearest_in_circuit(df, bad & df["circuit_id"].isin(set(clean["dim_circuito"]["circuit_id"])),
                                       clean["dim_transformador"])
            df.loc[best.index, "transformer_id"] = best
            rep("fk_repaired:transformer_id<-nearest_in_circuit(GIS)", len(best))
        tz = clean["dim_transformador"].set_index("transformer_id")
        known = df["transformer_id"].isin(tz.index)
        for c in ("circuit_id", "zone_id"):
            new = df.loc[known, "transformer_id"].map(tz[c]).astype("int64")
            rep(f"fk_repaired:{c}<-transformer", (new != df.loc[known, c]).sum())
            df.loc[known, c] = new
    elif name == "fact_pago":                               # el cliente se hereda de la factura
        inv = clean["fact_facturacion"].set_index("invoice_id")["client_id"]
        df["client_id"] = df["invoice_id"].map(inv).fillna(df["client_id"]).astype("int64")
    return df


def _clean_table(name, df, spec, clean, log, audit):
    df = df.copy()
    rep = lambda rule, n: audit.append({"table": name, "rule": rule, "rows": int(n)}) if n else None
    for c in [c for c in df.columns if _is_text(df[c])]:                       # 1. texto
        df[c] = df[c].astype("string").str.strip()
    for c in spec.get("ids", []):                                              # 2. enteros
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df = _reject(df, df[c].isna(), f"null_or_invalid_id:{c}", log)
        df[c] = df[c].astype("int64")
    for c, valid in spec.get("cats", {}).items():                              # 3. categorías
        canon = {v.lower(): v for v in valid}
        df[c] = df[c].str.lower().map(canon)
        df = _reject(df, df[c].isna(), f"invalid_category:{c}", log)
    for c in spec.get("dates", []):                                            # 4. fechas: reparar o rechazar
        df[c] = _parse_dates(df[c])
        bad = df[c].isna()
        fix = spec.get("date_fix", {}).get(c)
        if bad.any() and fix is not None:
            df.loc[bad, c] = fix(df).loc[bad]
            rep(f"date_repaired:{c}", bad.sum())
            bad = df[c].isna()
        if c not in spec.get("date_null", []):
            df = _reject(df, bad, f"invalid_date:{c}", log)
    df = _recover_fks(name, df, clean, rep)                                    # 5. llaves recuperables
    if name == "fact_pago":                                                    # fecha de pago inválida: imputar y marcar
        inv_date = df["invoice_id"].map(clean["fact_facturacion"].set_index("invoice_id")["invoice_date"])
        lag = int((df["payment_date"] - inv_date).dt.days.median())            # rezago mediano factura -> pago
        miss = df["payment_date"].isna() & inv_date.notna()
        df.loc[miss, "payment_date"] = inv_date[miss] + pd.Timedelta(days=lag)
        df["is_date_imputed"] = miss
        rep(f"date_imputed:payment_date(invoice_date+{lag}d, mediana)", miss.sum())
    for c in spec.get("nums", []):                                             # 6. numéricos
        df[c] = pd.to_numeric(df[c], errors="coerce")
        neg = df[c] < 0
        df.loc[neg, c] = df.loc[neg, c].abs()                                  # error de signo
        rep(f"sign_fixed:{c}", neg.sum())
        if spec.get("group"):
            med = df.groupby(spec["group"])[c].transform("median")
            out = df[c] > 8 * med
            df.loc[out, c] = np.nan                                            # outlier -> se repara/imputa abajo
            rep(f"outlier_nulled:{c}", out.sum())
    if name == "fact_lectura_energia":                                         # 7. imputación documentada
        lei = df["reading_status"] == "Leído"                                  # leído: registrado == consumo
        df.loc[lei, "registered_kwh"] = df.loc[lei, "registered_kwh"].fillna(df.loc[lei, "consumption_kwh"])
        df.loc[lei, "consumption_kwh"] = df.loc[lei, "consumption_kwh"].fillna(df.loc[lei, "registered_kwh"])
        med = df.groupby("client_id")["consumption_kwh"].transform("median")
        miss = df["consumption_kwh"].isna()
        df.loc[miss, "consumption_kwh"] = med[miss].fillna(df["consumption_kwh"].median())
        rep("imputed:consumption_kwh(mediana del cliente)", miss.sum())
        relabel = miss & (df["reading_status"] == "Leído")
        df.loc[relabel, "reading_status"] = "Estimado"
        rep("status_relabelled:Leído->Estimado", relabel.sum())
        df.loc[df["reading_status"] != "Leído", "ami_valid"] = False
    if name == "fact_facturacion":
        L = clean["fact_lectura_energia"].set_index("reading_id")
        cons = df["reading_id"].map(L["consumption_kwh"])                      # kWh facturados = consumo de la lectura
        miss = df["billed_kwh"].isna()
        df.loc[miss, "billed_kwh"] = cons[miss]
        rep("billed_kwh<-reading.consumption", (miss & cons.notna()).sum())
        tariff = df["client_id"].map(clean["dim_cliente"].set_index("client_id")["tariff_id"])
        rate = tariff.map(clean["dim_tarifa"].set_index("tariff_id")["rate_per_kwh"])
        calc = (df["billed_kwh"] * rate + FIXED_CHARGE).round(2)               # monto = kWh x tarifa + cargo fijo
        fix = df["amount_due"].isna() & calc.notna()
        df.loc[fix, "amount_due"] = calc[fix]
        rep("amount_due<-kWh*tarifa+cargo_fijo", fix.sum())
        st = df["reading_id"].map(L["reading_status"])                         # is_estimated = estado final de la lectura
        new = st != "Leído"
        chg = st.notna() & (df["is_estimated"].astype(bool) != new)
        df.loc[chg, "is_estimated"] = new[chg]
        rep("is_estimated_synced<-reading_status", chg.sum())
    for c in [c for c in spec.get("nums", []) if c not in spec.get("nullable", [])]:
        miss = df[c].isna()
        if miss.any():
            fill = (df.groupby(spec["group"])[c].transform("median") if spec.get("group")
                    else pd.Series(df[c].median(), index=df.index))
            df.loc[miss, c] = fill[miss].fillna(df[c].median())
            rep(f"imputed:{c}(mediana del grupo)", miss.sum())
    df = _reject(df, df.duplicated(spec["pk"], keep="first"), "duplicate_pk", log)          # 8. duplicados
    for c, parent in spec.get("fks", []):                                      # 9. integridad referencial
        bad = ~df[c].isin(set(clean[parent][c]))
        if c in spec.get("unk", []) and parent in UNKNOWN:
            df.loc[bad, c] = -1
            rep(f"fk_unknown(-1):{c}->{parent}", bad.sum())
        else:
            df = _reject(df, bad, f"orphan_fk:{c}->{parent}", log)
    if spec.get("biz"):                                                        # 9b. llave de negocio única
        df = _reject(df, df.duplicated(spec["biz"], keep="first"), "duplicate_business_key", log)
    if name == "fact_interrupcion":                                            # 10. reglas de negocio
        bad = df["end_datetime"] < df["start_datetime"]
        df.loc[bad, "end_datetime"] = (df["start_datetime"] + pd.to_timedelta(df["duration_minutes"], unit="m"))[bad]
        rep("end_before_start_repaired", bad.sum())
        df["duration_minutes"] = ((df["end_datetime"] - df["start_datetime"]).dt.total_seconds() // 60).astype(int)
    if name == "fact_pago":
        amt = clean["fact_facturacion"].set_index("invoice_id")["amount_due"]
        df = _reject(df, df["payment_amount"] > df["invoice_id"].map(amt) * 1.01, "overpayment", log)
    return df.reset_index(drop=True)


def _fill_energy_gaps(df, clean, audit):
    """Completa la rejilla transformador x día: los huecos (filas en cuarentena) se interpolan y se marcan."""
    t = clean["dim_transformador"]
    t = t.loc[t["transformer_id"] > 0, "transformer_id"]
    dk = clean["dim_tiempo"]["date_key"]
    dk = dk[(dk >= df["date_key"].min()) & (dk <= df["date_key"].max())].sort_values()
    grid = pd.MultiIndex.from_product([t, dk], names=["transformer_id", "date_key"]).to_frame(index=False)
    full = grid.merge(df, on=["transformer_id", "date_key"], how="left")
    miss = full["energy_record_id"].isna()
    for c in ("injected_kwh", "delivered_kwh"):
        full[c] = full.groupby("transformer_id")[c].transform(lambda s: s.interpolate(limit_direction="both"))
    full["date"] = _from_key(full)
    start = int(df["energy_record_id"].max()) + 1
    full.loc[miss, "energy_record_id"] = np.arange(start, start + miss.sum())
    full["energy_record_id"] = full["energy_record_id"].astype("int64")
    full["is_imputed"] = miss
    if miss.any():
        audit.append({"table": "fact_energia_transformador_diaria", "rule": "gap_filled(interpolación)",
                      "rows": int(miss.sum())})
    return full[df.columns.tolist() + ["is_imputed"]]


# =====================================================================
# ETL
# =====================================================================
def run_etl(raw_dir="data/raw", rejected_dir="data/rejected"):
    """Lee los CSV sucios y devuelve (tablas_limpias, resumen_por_tabla, auditoría_de_reglas)."""
    raw, rej = Path(raw_dir), Path(rejected_dir)
    rej.mkdir(parents=True, exist_ok=True)
    clean, report, audit = {}, [], []
    for name, spec in SPEC.items():                         # el orden del dict respeta padres -> hijos
        df = pd.read_csv(raw / f"{name}.csv")
        log = []
        out = _clean_table(name, df, spec, clean, log, audit)
        if name == "fact_energia_transformador_diaria":
            out = _fill_energy_gaps(out, clean, audit)
        n_data = len(out)
        if name in UNKNOWN:
            out = pd.concat([out, pd.DataFrame([UNKNOWN[name]])], ignore_index=True)
        clean[name] = out
        rejected = pd.concat(log, ignore_index=True, sort=False) if log else pd.DataFrame()
        if len(rejected):
            rejected.to_csv(rej / f"{name}_rejected.csv", index=False)
        n_imp = int(out["is_imputed"].sum()) if "is_imputed" in out else 0
        report.append({"table": name, "raw_rows": len(df), "clean_rows": n_data - n_imp,
                       "gap_filled_rows": n_imp, "rejected_rows": len(rejected),
                       "reasons": rejected["rejection_reason"].value_counts().to_dict() if len(rejected) else {}})
        print(f"{name}: {len(df):,} -> {n_data:,}")
    return clean, pd.DataFrame(report), pd.DataFrame(audit, columns=["table", "rule", "rows"])


# =====================================================================
# Verificación de los criterios del caso (sobre los datos ya limpios)
# =====================================================================
def _portfolio(clean, asof):
    """Cartera al corte `asof`: saldo = facturado - pagos <= corte, para facturas emitidas <= corte."""
    f = clean["fact_facturacion"]
    f = f[f["invoice_date"] <= asof]
    p = clean["fact_pago"]
    p = p[p["payment_date"] <= asof].groupby("invoice_id")["payment_amount"].sum()
    bal = (f["amount_due"] - f["invoice_id"].map(p).fillna(0)).clip(lower=0)
    over60 = bal[(asof - f["due_date"]).dt.days > 60].sum()
    return bal.sum(), over60


def _cohort_overdue60(clean, months, asof=pd.Timestamp("2025-12-31")):
    """% del monto facturado que seguía impago 60 días después del vencimiento (cohortes de emisión)."""
    f = clean["fact_facturacion"]
    f = f[f["period_month"].isin(months) & (f["due_date"] + pd.Timedelta(days=60) <= asof)]
    p = clean["fact_pago"].merge(f[["invoice_id", "due_date"]], on="invoice_id")
    p = p[p["payment_date"] <= p["due_date"] + pd.Timedelta(days=60)].groupby("invoice_id")["payment_amount"].sum()
    unpaid = (f["amount_due"] - f["invoice_id"].map(p).fillna(0)).clip(lower=0)
    return unpaid.sum() / f["amount_due"].sum()


def verify_requirements(clean, report=None):
    """Tablero de cumplimiento: criterios de datos del caso (cantidades, calibración y calidad)."""
    cl, med = clean["dim_cliente"], clean["dim_medidor"]
    lec, fac, ene = clean["fact_lectura_energia"], clean["fact_facturacion"], clean["fact_energia_transformador_diaria"]
    inc = clean["fact_interrupcion"]
    month = ene["date"].dt.month
    inj = ene.groupby(month)["injected_kwh"].sum()
    bil = fac.groupby("period_month")["billed_kwh"].sum()
    loss = 1 - bil / inj
    t1, t2 = loss.loc[[1, 2, 3]].mean(), loss.loc[[10, 11, 12]].mean()
    c1, c3 = _cohort_overdue60(clean, [1, 2, 3]), _cohort_overdue60(clean, [7, 8, 9])
    jun, dec = _portfolio(clean, pd.Timestamp("2025-06-30")), _portfolio(clean, pd.Timestamp("2025-12-31"))
    act = med[(med["meter_id"] > 0) & (med["status"] == "Activo")]
    ami = act["ami_enabled"].astype(bool).mean()
    orphan_inv = (~fac["reading_id"].isin(lec["reading_id"])).sum()
    pay_q = None
    if report is not None:                                  # pagos NO aplicados (huérfanos/sobrepago) vs pagos cargados
        reasons = report.loc[report["table"] == "fact_pago", "reasons"].iloc[0]
        reasons = eval(reasons) if isinstance(reasons, str) else reasons
        pay_q = sum(v for k, v in reasons.items() if k != "duplicate_pk") / len(clean["fact_pago"])
    rows = [
        ("Clientes >= 50.000", f"{(cl['client_id'] > 0).sum():,}", (cl["client_id"] > 0).sum() >= 50_000),
        ("12 meses de facturación", f"{fac['period_month'].nunique()} meses", fac["period_month"].nunique() == 12),
        ("Lecturas >= 500.000", f"{len(lec):,}", len(lec) >= 500_000),
        ("Interrupciones georreferenciadas", f"{inc['latitude'].notna().mean():.0%} con lat/lon",
         inc["latitude"].notna().mean() > .95),
        ("Medidores AMI ~72%", f"{ami:.1%}", abs(ami - .72) < .03),
        ("Pérdida T1 -> T4 (caso: 9,4% -> 13,1%)", f"{t1:.1%} -> {t2:.1%}",
         abs(t1 - .094) < .006 and abs(t2 - .131) < .006),
        ("Morosidad >60d, cohorte T3 vs T1 (caso: +26%)", f"{c1:.1%} -> {c3:.1%} ({c3 / c1 - 1:+.0%})",
         0.15 <= c3 / c1 - 1 <= 0.40),
        ("Cartera >60d / cartera total (jun -> dic)", f"{jun[1] / jun[0]:.1%} -> {dec[1] / dec[0]:.1%}",
         dec[1] / dec[0] > jun[1] / jun[0]),
        ("Lecturas estimadas / faltantes", f"{(lec['reading_status'] == 'Estimado').mean():.1%} / "
         f"{(lec['reading_status'] == 'Faltante').mean():.1%}", True),
        ("Sin duplicados de negocio (cliente-periodo)", "0", not lec.duplicated(
            ["client_id", "period_year", "period_month"]).any()),
        ("Toda factura referencia una lectura válida", f"{orphan_inv} huérfanas", orphan_inv == 0),
    ]
    if pay_q is not None:
        rows.append(("Pagos en cuarentena (huérfanos/sobrepago) < 3%", f"{pay_q:.1%} de los pagos", pay_q < .03))
    out = pd.DataFrame(rows, columns=["criterio", "valor", "cumple"])
    print(out.to_string(index=False))
    return out


# =====================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EnerGrid ETL")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--rejected", default="data/rejected")
    ap.add_argument("--no-load", action="store_true", help="solo ETL, sin cargar a SQL Server")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="EnerGrid")
    ap.add_argument("--user", default=None, help="usuario SQL opcional; sin el usa la sesion de Windows")
    ap.add_argument("--password", default=None, help="omitir para pedirla de forma oculta si se usa --user")
    a = ap.parse_args()
    clean_, report_, audit_ = run_etl(a.raw, a.rejected)
    verify_requirements(clean_, report_)
    if not a.no_load:
        if a.user and a.password is None:
            from getpass import getpass
            a.password = getpass("Contraseña SQL: ")
        from energrid_sqlserver import load_sqlserver
        load_sqlserver(clean_, a.server, a.database, user=a.user, password=a.password,
                       report=report_.assign(reasons=report_["reasons"].astype(str)), audit=audit_)
