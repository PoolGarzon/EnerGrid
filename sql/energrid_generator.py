"""EnerGrid - Generador de datos sinteticos para pruebas y calibracion."""
from pathlib import Path

import numpy as np
import pandas as pd

from energrid_config import (CLIENT_TYPES, DUE_DAYS, FIXED_CHARGE, INVOICE_DAY, PAY_METHODS,
                             READING_DAY, REGIONS)


# =====================================================================
# Ruido
# =====================================================================
def _add_noise(df, rng, rate, text_cols=(), num_cols=(), null_cols=(),
               date_cols=None, fk_cols=(), dup=True, dup_newid=None):
    """Inyecta: texto sucio, negativos, outliers, nulos, fechas inválidas/otro formato, llaves foráneas
    huérfanas, filas duplicadas exactas y (si dup_newid) duplicados con nuevo id sobre la misma llave de negocio."""
    df = df.copy()
    n = len(df)
    mask = lambda r: rng.random(n) < r

    for c in text_cols:
        df[c] = df[c].astype(object)
        m = mask(rate * 2)
        vals = df.loc[m, c].astype(str).tolist()
        k = rng.integers(0, 3, len(vals))
        df.loc[m, c] = [v.upper() if i == 0 else f"  {v} " if i == 1 else v.lower()
                        for v, i in zip(vals, k)]
    for c in num_cols:
        df[c] = df[c].astype(float)
        df.loc[mask(rate / 2), c] *= -1          # valores negativos
        df.loc[mask(rate / 4), c] *= 50          # outliers
    for c in null_cols:
        df[c] = df[c].astype(float)
        df.loc[mask(rate), c] = np.nan
    for c, fmt in (date_cols or {}).items():
        s = df[c].dt.strftime(fmt).astype(object)
        m = mask(rate)                           # formato dd/mm/yyyy
        s[m] = df.loc[m, c].dt.strftime(fmt.replace("%Y-%m-%d", "%d/%m/%Y"))
        m = mask(rate / 2)                       # fechas inválidas
        s[m] = rng.choice(["31/02/2025", "N/A", "2025-13-45", "00/00/0000"], m.sum())
        df[c] = s
    for c in fk_cols:
        df.loc[mask(rate / 2), c] = 9_999_999    # huérfanos
    parts = [df]
    if dup:
        parts.append(df.sample(frac=rate / 2, random_state=int(rng.integers(1_000_000_000))))
    if dup_newid:                                # reenvío del mismo hecho con otro id (no usa rng: no altera la semilla)
        extra = df.iloc[:n].sample(frac=rate / 2, random_state=n)
        extra[dup_newid] = int(df[dup_newid].max()) + 1 + np.arange(len(extra))
        parts.append(extra)
    return pd.concat(parts, ignore_index=True) if len(parts) > 1 else df


# =====================================================================
# Generador
# =====================================================================
def generate_noisy_data(output_dir="data/raw", seed=20260910, year=2025, n_clients=50_000,
                        n_zones=15, n_circuits=500, n_transformers=2_500,
                        outages_per_month=1_200, noise_rate=0.02, ami_rate=0.72,
                        unpaid_rate=0.08, partial_rate=0.18, hot_share=0.08, unpaid_growth=2.0,
                        loss_base=(0.056, 0.096), loss_trend=0.049, missing_bias=(0.70, 1.00),
                        save=True):
    """Genera todas las tablas del modelo dimensional (con ruido) y las guarda en CSV."""
    assert n_clients >= n_transformers >= n_circuits, "Debe cumplirse clientes>=transformadores>=circuitos"
    rng = np.random.default_rng(seed)
    rng_x = np.random.default_rng(seed + 1)          # flujo aparte: no altera los datos base
    n, M = n_clients, 12

    # ---------- Geografía: Región -> Zona -> Circuito -> Transformador ----------
    zone_lat = rng.uniform(-0.40, 0.05, n_zones)
    zone_lon = rng.uniform(-78.75, -78.35, n_zones)
    zona = pd.DataFrame({"zone_id": np.arange(1, n_zones + 1),
                         "zone_name": [f"Zona {i:02d}" for i in range(1, n_zones + 1)],
                         "region": [REGIONS[i % 4] for i in range(n_zones)]})
    circ_zone = rng.integers(1, n_zones + 1, n_circuits)
    circuito = pd.DataFrame({"circuit_id": np.arange(1, n_circuits + 1),
                             "circuit_name": [f"CIR-{i:04d}" for i in range(1, n_circuits + 1)],
                             "zone_id": circ_zone,
                             "voltage_level": rng.choice(["Baja", "Media"], n_circuits, p=[.7, .3])})
    tr_circ = np.concatenate([np.arange(1, n_circuits + 1),
                              rng.integers(1, n_circuits + 1, n_transformers - n_circuits)])
    tr_zone = circ_zone[tr_circ - 1]
    tr_lat = zone_lat[tr_zone - 1] + rng.normal(0, .02, n_transformers)
    tr_lon = zone_lon[tr_zone - 1] + rng.normal(0, .02, n_transformers)
    transf = pd.DataFrame({"transformer_id": np.arange(1, n_transformers + 1),
                           "transformer_name": [f"TR-{i:05d}" for i in range(1, n_transformers + 1)],
                           "circuit_id": tr_circ, "zone_id": tr_zone,
                           "capacity_kva": rng.choice([25, 50, 75, 100, 150, 225, 300], n_transformers),
                           "latitude": np.round(tr_lat, 6), "longitude": np.round(tr_lon, 6)})
    tarifa = pd.DataFrame({"tariff_id": [1, 2, 3, 4],
                           "tariff_name": ["Residencial", "Comercial", "Industrial", "Social"],
                           "rate_per_kwh": [0.095, 0.125, 0.105, 0.065]})
    rates = tarifa["rate_per_kwh"].to_numpy()
    causa = pd.DataFrame({"cause_id": range(1, 7),
                          "cause_name": ["Clima", "Falla de equipo", "Mantenimiento",
                                         "Vegetación", "Accidente externo", "Sobrecarga"],
                          "planned": [False, False, True, False, False, False]})
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    tdays = pd.date_range(f"{year}-01-01", f"{year + 1}-03-31", freq="D")      # + vencimientos de ene-mar
    tiempo = pd.DataFrame({"date_key": tdays.strftime("%Y%m%d").astype(int), "date": tdays,
                           "year": tdays.year, "quarter": tdays.quarter, "month": tdays.month,
                           "month_name": tdays.month_name(), "day": tdays.day,
                           "day_of_week": tdays.dayofweek + 1, "is_weekend": tdays.dayofweek >= 5})

    # ---------- Clientes (heredan circuito y zona de su transformador) ----------
    c_tr = np.concatenate([np.arange(1, n_transformers + 1),
                           rng.integers(1, n_transformers + 1, n - n_transformers)])
    rng.shuffle(c_tr)
    c_circ, c_zone = tr_circ[c_tr - 1], tr_zone[c_tr - 1]
    ctype = rng.choice(CLIENT_TYPES, n, p=[.82, .15, .03])
    c_tariff = np.where(ctype == "Residencial", np.where(rng.random(n) < .12, 4, 1),
                        np.where(ctype == "Comercial", 2, 3))
    d0 = pd.Timestamp("2010-01-01")
    span = (pd.Timestamp(f"{year}-01-01") - d0).days
    conn_date = d0 + pd.to_timedelta(rng.integers(0, span, n), unit="D")
    cliente = pd.DataFrame({"client_id": np.arange(1, n + 1),
                            "client_code": [f"CLI-{i:06d}" for i in range(1, n + 1)],
                            "client_type": ctype, "tariff_id": c_tariff, "zone_id": c_zone,
                            "circuit_id": c_circ, "transformer_id": c_tr,
                            "latitude": np.round(tr_lat[c_tr - 1] + rng.normal(0, .002, n), 6),
                            "longitude": np.round(tr_lon[c_tr - 1] + rng.normal(0, .002, n), 6),
                            "active": rng.random(n) < .97, "connection_date": conn_date})

    # ---------- Medidores (72% AMI; 3% reemplazados durante el año) ----------
    ami = rng.random(n) < ami_rate
    replaced = rng.random(n) < .03
    repl_month = np.where(replaced, rng.integers(3, 11, n), 0)
    rep_idx = np.where(replaced)[0]
    new_meter = np.zeros(n, dtype=int)
    new_meter[rep_idx] = n + 1 + np.arange(len(rep_idx))
    meters_main = pd.DataFrame({"meter_id": np.arange(1, n + 1),
                                "meter_serial": [f"MED-{i:07d}" for i in range(1, n + 1)],
                                "client_id": np.arange(1, n + 1),
                                "meter_type": np.where(ami, "Inteligente", "Convencional"),
                                "ami_enabled": ami, "installed_date": conn_date,
                                "status": np.where(replaced, "Reemplazado", "Activo")})
    meters_new = pd.DataFrame({"meter_id": new_meter[rep_idx],
                               "meter_serial": [f"MED-{i:07d}" for i in new_meter[rep_idx]],
                               "client_id": rep_idx + 1, "meter_type": "Inteligente",
                               "ami_enabled": True,
                               "installed_date": pd.to_datetime([f"{year}-{m:02d}-01" for m in repl_month[rep_idx]]),
                               "status": "Activo"})
    medidor = pd.concat([meters_main, meters_new], ignore_index=True)

    # ---------- Lecturas mensuales (cliente x mes) ----------
    N = n * M
    cidx = np.repeat(np.arange(n), M)
    m = np.tile(np.arange(1, M + 1), n)
    base = np.select([ctype == "Residencial", ctype == "Comercial"], [145, 520], 2800)[cidx]
    actual = np.maximum(8, rng.lognormal(np.log(base * (1 + .08 * np.sin(2 * np.pi * (m - 1) / 12))), .28))
    ami_c = ami[cidx]
    # fallas de comunicación AMI concentradas por zona (media 1.0 para conservar la tasa global)
    zone_tele = rng_x.uniform(.4, 2.2, n_zones)
    zone_tele = zone_tele / zone_tele.mean()
    miss = rng.random(N) < np.where(ami_c, .04, .01) * zone_tele[c_zone[cidx] - 1]   # telemetría caída
    est = ~miss & (rng.random(N) < np.where(ami_c, .02, .15))        # lectura estimada
    status = np.where(miss, "Faltante", np.where(est, "Estimado", "Leído"))
    registered = np.where(status == "Leído", actual, np.nan)
    u = rng.uniform(.85, 1.15, N)
    factor = np.where(status == "Faltante", missing_bias[0] + (u - .85) * (missing_bias[1] - missing_bias[0]) / .30, u)
    estimated = np.where(status != "Leído", actual * factor, np.nan)
    consumption = np.where(status == "Leído", actual, estimated)
    rdate = pd.date_range(f"{year}-01-01", periods=M, freq="MS")[m - 1] + pd.Timedelta(days=READING_DAY - 1)
    meter_id = np.where((repl_month[cidx] > 0) & (m >= repl_month[cidx]), new_meter[cidx], cidx + 1)
    lectura = pd.DataFrame({"reading_id": np.arange(1, N + 1), "client_id": cidx + 1,
                            "meter_id": meter_id, "reading_date": rdate,
                            "date_key": rdate.strftime("%Y%m%d").astype(int),
                            "period_year": year, "period_month": m,
                            "registered_kwh": np.round(registered, 3),
                            "estimated_kwh": np.round(estimated, 3),
                            "consumption_kwh": np.round(consumption, 3),
                            "reading_status": status, "ami_valid": status == "Leído"})

    # ---------- Facturación ----------
    amount = np.round(consumption * rates[c_tariff[cidx] - 1] + FIXED_CHARGE, 2)
    inv_date = rdate + pd.Timedelta(days=INVOICE_DAY - READING_DAY)
    due_date = inv_date + pd.Timedelta(days=DUE_DAYS)
    factura = pd.DataFrame({"invoice_id": np.arange(1, N + 1), "client_id": cidx + 1,
                            "reading_id": lectura["reading_id"], "invoice_date": inv_date,
                            "due_date": due_date, "period_year": year, "period_month": m,
                            "billed_kwh": np.round(consumption, 3),
                            "is_estimated": status != "Leído", "amount_due": amount})

    # ---------- Pagos (morosidad distinta por zona; pagos parciales y divididos) ----------
    zone_unpaid = rng.uniform(unpaid_rate / 2, unpaid_rate * 2.5, n_zones)
    growth = np.where(rng.random(n_zones) < .3, rng.uniform(.8, 1.6, n_zones), rng.uniform(0, .25, n_zones))
    zz = c_zone[cidx] - 1
    p_unpaid = np.minimum(.6, zone_unpaid[zz] * (1 + unpaid_growth * growth[zz] * (m - 1) / 11))   # morosidad creciente en zonas críticas
    unpaid = rng.random(N) < p_unpaid
    late = unpaid & (rng.random(N) < .55)                              # regularizan tarde (61-200 días)
    partial = ~unpaid & (rng.random(N) < partial_rate)
    ratio = np.where(unpaid & ~late, 0.0, np.where(partial, rng.uniform(.2, .85, N), 1.0))
    paid = np.round(amount * ratio, 2)
    two = (ratio > 0) & ~late & (rng.random(N) < .15)
    lag1 = np.where(late, rng.integers(61, 200, N), rng.integers(0, 45, N))
    first = np.where(two, np.round(paid * rng.uniform(.4, .7, N), 2), paid)
    idx1 = np.where(ratio > 0)[0]
    idx2 = np.where(two)[0]
    meth = lambda k: rng.choice(PAY_METHODS, k, p=[.55, .45])
    p1 = pd.DataFrame({"invoice_id": idx1 + 1, "client_id": cidx[idx1] + 1,
                       "payment_date": inv_date[idx1] + pd.to_timedelta(lag1[idx1], unit="D"),
                       "payment_amount": first[idx1], "payment_method": meth(len(idx1))})
    p2 = pd.DataFrame({"invoice_id": idx2 + 1, "client_id": cidx[idx2] + 1,
                       "payment_date": inv_date[idx2] + pd.to_timedelta(rng.integers(30, 75, len(idx2)), unit="D"),
                       "payment_amount": np.round(paid[idx2] - first[idx2], 2), "payment_method": meth(len(idx2))})
    pago = pd.concat([p1, p2], ignore_index=True)
    pago = pago[pago["payment_date"] <= pd.Timestamp(f"{year}-12-31")]   # sin pagos futuros
    pago = pago.sort_values(["invoice_id", "payment_date"], ignore_index=True)
    pago.insert(0, "payment_id", np.arange(1, len(pago) + 1))

    # ---------- Energía por transformador/día (reconciliable con facturación) ----------
    key = (c_tr[cidx] - 1) * M + (m - 1)
    billed_tm = np.bincount(key, weights=actual, minlength=n_transformers * M).reshape(n_transformers, M)
    D = len(days)
    day_keys = days.strftime("%Y%m%d").astype(int).to_numpy()
    dm = days.month.to_numpy() - 1
    delivered = billed_tm[:, dm] / days.days_in_month.to_numpy()[None, :] * rng.lognormal(0, .05, (n_transformers, D))
    base_loss = rng.uniform(loss_base[0], loss_base[1], n_transformers)
    anom_c = rng.random(n_circuits) < .05                              # circuitos con pérdida anómala (clave de respuesta)
    anom_t = anom_c[tr_circ - 1]
    base_loss[anom_t] += rng.uniform(.08, .15, int(anom_t.sum()))
    loss = np.clip(base_loss[:, None] + loss_trend * np.arange(D)[None, :] / D
                   + rng.normal(0, .01, (n_transformers, D)), .02, .45)
    injected = delivered / (1 - loss)
    energia = pd.DataFrame({"energy_record_id": np.arange(1, n_transformers * D + 1),
                            "date_key": np.tile(day_keys, n_transformers),
                            "date": np.tile(days.to_numpy(), n_transformers),
                            "transformer_id": np.repeat(np.arange(1, n_transformers + 1), D),
                            "injected_kwh": np.round(injected.ravel(), 3),
                            "delivered_kwh": np.round(delivered.ravel(), 3)})

    # ---------- Interrupciones georreferenciadas ----------
    k = outages_per_month * M
    n_cli_all = np.bincount(c_tr, minlength=n_transformers + 1)
    load = n_cli_all[1:] / transf["capacity_kva"].to_numpy()
    hot = load >= np.quantile(load, 1 - hot_share)                     # transformadores saturados (clave de respuesta)
    w = np.where(hot, 8.0, 1.0)
    t_ids = rng.choice(np.arange(1, n_transformers + 1), k, p=w / w.sum())
    start = pd.Timestamp(f"{year}-01-01") + pd.to_timedelta(
        rng.integers(0, 365 * 86400, k), unit="s")
    dur = np.clip(rng.lognormal(np.log(80), .7, k), 5, 720).astype(int)
    cid = rng.choice(causa["cause_id"], k, p=[.25, .25, .15, .15, .1, .1])
    cid[hot[t_ids - 1] & (rng.random(k) < .55)] = 6                    # sobrecarga domina en saturados
    n_cli_tr = n_cli_all[t_ids]
    end = start + pd.to_timedelta(dur, unit="m")
    end = end.where(rng.random(k) > noise_rate / 2, start - pd.Timedelta(minutes=30))  # fin < inicio
    interr = pd.DataFrame({"outage_id": np.arange(1, k + 1), "start_datetime": start,
                           "end_datetime": end,
                           "date_key": start.strftime("%Y%m%d").astype(int),
                           "circuit_id": tr_circ[t_ids - 1], "transformer_id": t_ids,
                           "zone_id": tr_zone[t_ids - 1], "cause_id": cid,
                           "duration_minutes": dur,
                           "customers_affected": np.maximum(1, (n_cli_tr * rng.uniform(.3, 1, k)).astype(int)),
                           "planned": np.isin(cid, causa.loc[causa["planned"], "cause_id"]),
                           "latitude": np.round(tr_lat[t_ids - 1] + rng.normal(0, .001, k), 6),
                           "longitude": np.round(tr_lon[t_ids - 1] + rng.normal(0, .001, k), 6)})

    # ---------- Ruido ----------
    D_ = "%Y-%m-%d"
    r = noise_rate
    tables = {
        "dim_zona": zona, "dim_circuito": circuito, "dim_transformador": transf,
        "dim_tarifa": tarifa, "dim_causa": causa,
        "dim_tiempo": tiempo.assign(date=tiempo["date"].dt.strftime(D_)),
        "dim_cliente": _add_noise(cliente, rng, r, text_cols=["client_type"],
                                  date_cols={"connection_date": D_},
                                  fk_cols=["transformer_id", "tariff_id"]),
        "dim_medidor": _add_noise(medidor, rng, r, text_cols=["meter_type", "status"],
                                  date_cols={"installed_date": D_}, fk_cols=["client_id"]),
        "fact_lectura_energia": _add_noise(lectura, rng, r, text_cols=["reading_status"],
                                           num_cols=["registered_kwh", "consumption_kwh"],
                                           null_cols=["consumption_kwh"],
                                           date_cols={"reading_date": D_},
                                           fk_cols=["client_id", "meter_id"], dup_newid="reading_id"),
        "fact_facturacion": _add_noise(factura, rng, r, num_cols=["billed_kwh", "amount_due"],
                                       date_cols={"invoice_date": D_, "due_date": D_},
                                       fk_cols=["client_id", "reading_id"], dup_newid="invoice_id"),
        "fact_pago": _add_noise(pago, rng, r, text_cols=["payment_method"],
                                num_cols=["payment_amount"], date_cols={"payment_date": D_},
                                fk_cols=["invoice_id"]),
        "fact_energia_transformador_diaria": _add_noise(energia, rng, r,
                                                        num_cols=["injected_kwh", "delivered_kwh"],
                                                        date_cols={"date": D_}, fk_cols=["transformer_id"]),
        "fact_interrupcion": _add_noise(interr, rng, r,
                                        date_cols={"start_datetime": D_ + " %H:%M:%S",
                                                   "end_datetime": D_ + " %H:%M:%S"},
                                        fk_cols=["circuit_id", "transformer_id", "cause_id"]),
    }
    answer_key = {
        "circuitos_perdida_anomala": pd.DataFrame({"circuit_id": np.where(anom_c)[0] + 1}),
        "transformadores_saturados": pd.DataFrame({"transformer_id": np.where(hot)[0] + 1}),
        "zonas_morosidad": pd.DataFrame({"zone_id": np.arange(1, n_zones + 1),
                                         "morosidad_base": zone_unpaid.round(3), "crecimiento": (growth * unpaid_growth).round(2)}),
        "zonas_telemetria": pd.DataFrame({"zone_id": np.arange(1, n_zones + 1),
                                          "factor_falla_comunicacion": zone_tele.round(2)}),
    }
    if save:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, df in tables.items():
            df.to_csv(out / f"{name}.csv", index=False)
        kd = out.parent / "answer_key"                                  # NO se usa en el dashboard; sirve para validar
        kd.mkdir(parents=True, exist_ok=True)
        for name, df in answer_key.items():
            df.to_csv(kd / f"{name}.csv", index=False)
        print(f"RAW generado en {out.resolve()}: "
              f"{len(tables['dim_cliente']):,} clientes, {len(tables['fact_lectura_energia']):,} lecturas, "
              f"{len(tables['fact_facturacion']):,} facturas, {len(tables['fact_pago']):,} pagos, "
              f"{len(tables['fact_energia_transformador_diaria']):,} registros de energía, "
              f"{len(tables['fact_interrupcion']):,} interrupciones")
    return tables


if __name__ == "__main__":
    generate_noisy_data("data/raw")
