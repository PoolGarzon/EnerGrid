-- 03_vistas_kpis.sql: Vistas analiticas (KPIs, cartera, calidad, criticidad y alertas)
USE EnerGrid;
GO

-- 2.1 Factura enriquecida
CREATE OR ALTER VIEW [energrid].[vw_facturacion_detalle] AS
SELECT fa.invoice_id, fa.client_id, c.client_type, c.tariff_id, t.rate_per_kwh,
       c.zone_id, c.circuit_id, c.transformer_id,
       fa.period_year, fa.period_month, fa.invoice_date, fa.due_date,
       fa.billed_kwh, fa.amount_due, CAST(ISNULL(fa.is_estimated, 0) AS BIT) AS is_estimated,
       l.reading_status, l.meter_id
FROM [energrid].[fact_facturacion] fa
JOIN [energrid].[fact_lectura_energia] l ON l.reading_id = fa.reading_id
JOIN [energrid].[dim_cliente] c          ON c.client_id  = fa.client_id
JOIN [energrid].[dim_tarifa]  t          ON t.tariff_id  = c.tariff_id;
GO

-- 2.2 Clientes servidos (denominador de SAIDI/SAIFI)
CREATE OR ALTER VIEW [energrid].[vw_clientes_servidos] AS
SELECT zone_id, circuit_id, transformer_id, COUNT(*) AS clientes_servidos
FROM [energrid].[dim_cliente]
WHERE client_id > 0 AND active = 1
GROUP BY zone_id, circuit_id, transformer_id;
GO

-- 2.3 Patron de consumo del cliente
CREATE OR ALTER VIEW [energrid].[vw_cliente_patron_consumo] AS
WITH s AS (
    SELECT l.client_id, AVG(l.consumption_kwh) AS consumo_prom_kwh,
           STDEV(l.consumption_kwh) / NULLIF(AVG(l.consumption_kwh), 0) AS coef_variacion,
           AVG(CASE WHEN l.reading_status <> N'Leído' THEN 1.0 ELSE 0 END) AS pct_lecturas_no_medidas
    FROM [energrid].[fact_lectura_energia] l GROUP BY l.client_id
), q AS (
    SELECT s.*, c.client_type,
           NTILE(4) OVER (PARTITION BY c.client_type ORDER BY s.consumo_prom_kwh) AS cuartil_consumo
    FROM s JOIN [energrid].[dim_cliente] c ON c.client_id = s.client_id WHERE c.client_id > 0
)
SELECT client_id, client_type, consumo_prom_kwh, coef_variacion, pct_lecturas_no_medidas, cuartil_consumo,
       CASE cuartil_consumo WHEN 1 THEN N'Bajo' WHEN 2 THEN N'Medio-bajo' WHEN 3 THEN N'Medio-alto' ELSE N'Alto' END AS segmento_consumo,
       CASE WHEN coef_variacion > 0.35 THEN N'Volátil' ELSE N'Estable' END AS patron_variabilidad
FROM q;
GO

-- 3.1 Perdidas por transformador y mes
CREATE OR ALTER VIEW [energrid].[vw_perdida_transformador_mes] AS
WITH p AS (SELECT periodo_desde, periodo_hasta, perdida_tecnica_ref FROM [energrid].[cfg_parametros] WHERE id = 1),
e AS (
    SELECT t.transformer_id, t.circuit_id, t.zone_id, YEAR(f.[date]) AS period_year, MONTH(f.[date]) AS period_month,
           SUM(f.injected_kwh) AS injected_kwh, SUM(f.delivered_kwh) AS delivered_kwh,
           SUM(CASE WHEN f.is_imputed = 1 THEN f.injected_kwh ELSE 0 END) AS injected_kwh_imputado
    FROM [energrid].[fact_energia_transformador_diaria] f
    JOIN [energrid].[dim_transformador] t ON t.transformer_id = f.transformer_id
    CROSS JOIN p
    WHERE f.[date] BETWEEN p.periodo_desde AND p.periodo_hasta
    GROUP BY t.transformer_id, t.circuit_id, t.zone_id, YEAR(f.[date]), MONTH(f.[date])
), b AS (
    SELECT d.transformer_id, d.circuit_id, d.zone_id, d.period_year, d.period_month,
           SUM(d.billed_kwh) AS billed_kwh,
           SUM(CASE WHEN d.reading_status = N'Leído'    THEN d.billed_kwh ELSE 0 END) AS billed_kwh_leido,
           SUM(CASE WHEN d.reading_status = N'Estimado' THEN d.billed_kwh ELSE 0 END) AS billed_kwh_estimado,
           SUM(CASE WHEN d.reading_status = N'Faltante' THEN d.billed_kwh ELSE 0 END) AS billed_kwh_faltante,
           SUM(d.billed_kwh * d.rate_per_kwh) AS billed_energy_usd,
           SUM(d.amount_due) AS billed_usd
    FROM [energrid].[vw_facturacion_detalle] d CROSS JOIN p
    WHERE DATEFROMPARTS(d.period_year, d.period_month, 1) BETWEEN DATEFROMPARTS(YEAR(p.periodo_desde), MONTH(p.periodo_desde), 1) AND p.periodo_hasta
    GROUP BY d.transformer_id, d.circuit_id, d.zone_id, d.period_year, d.period_month
), j AS (
    SELECT COALESCE(e.transformer_id, b.transformer_id) AS transformer_id,
           COALESCE(e.circuit_id, b.circuit_id) AS circuit_id, COALESCE(e.zone_id, b.zone_id) AS zone_id,
           COALESCE(e.period_year, b.period_year) AS period_year, COALESCE(e.period_month, b.period_month) AS period_month,
           ISNULL(e.injected_kwh, 0) AS injected_kwh, ISNULL(e.delivered_kwh, 0) AS delivered_kwh,
           ISNULL(e.injected_kwh_imputado, 0) AS injected_kwh_imputado,
           ISNULL(b.billed_kwh, 0) AS billed_kwh, ISNULL(b.billed_kwh_leido, 0) AS billed_kwh_leido,
           ISNULL(b.billed_kwh_estimado, 0) AS billed_kwh_estimado, ISNULL(b.billed_kwh_faltante, 0) AS billed_kwh_faltante,
           ISNULL(b.billed_energy_usd, 0) AS billed_energy_usd, ISNULL(b.billed_usd, 0) AS billed_usd
    FROM e FULL JOIN b ON b.transformer_id = e.transformer_id AND b.circuit_id = e.circuit_id AND b.zone_id = e.zone_id
                      AND b.period_year = e.period_year AND b.period_month = e.period_month
)
SELECT j.transformer_id, j.circuit_id, j.zone_id, j.period_year, j.period_month,
       DATEFROMPARTS(j.period_year, j.period_month, 1) AS period_start,
       j.period_year * 10000 + j.period_month * 100 + 1 AS date_key_mes,
       j.injected_kwh, j.delivered_kwh, j.billed_kwh, j.injected_kwh_imputado,
       j.billed_kwh_leido, j.billed_kwh_estimado, j.billed_kwh_faltante, j.billed_usd,
       j.injected_kwh - j.billed_kwh                                   AS perdida_total_kwh,
       j.injected_kwh - j.delivered_kwh                                AS perdida_red_kwh,
       j.delivered_kwh - j.billed_kwh                                  AS brecha_medicion_kwh,
       x.tecnica                                                       AS perdida_tecnica_kwh,
       (j.injected_kwh - j.delivered_kwh) - x.tecnica                  AS perdida_no_tecnica_kwh,
       (j.injected_kwh - j.billed_kwh) / NULLIF(j.injected_kwh, 0)     AS perdida_total_pct,
       j.billed_energy_usd / NULLIF(j.billed_kwh, 0)                   AS precio_medio_usd_kwh,
       (j.injected_kwh - j.billed_kwh) * ISNULL(j.billed_energy_usd / NULLIF(j.billed_kwh, 0), 0)      AS perdida_total_usd,
       (j.injected_kwh - j.billed_kwh - x.tecnica) * ISNULL(j.billed_energy_usd / NULLIF(j.billed_kwh, 0), 0) AS perdida_evitable_usd
FROM j CROSS JOIN p
CROSS APPLY (SELECT CASE WHEN j.injected_kwh - j.delivered_kwh <= 0 THEN 0.0
                         WHEN j.injected_kwh - j.delivered_kwh < j.injected_kwh * p.perdida_tecnica_ref
                              THEN j.injected_kwh - j.delivered_kwh
                         ELSE j.injected_kwh * p.perdida_tecnica_ref END AS tecnica) x;
GO

-- 3.2 Perdidas agregadas por zona y mes
CREATE OR ALTER VIEW [energrid].[vw_perdida_zona_mes] AS
SELECT zone_id, period_start, SUM(injected_kwh) AS injected_kwh, SUM(delivered_kwh) AS delivered_kwh,
       SUM(billed_kwh) AS billed_kwh, SUM(perdida_total_kwh) AS perdida_total_kwh,
       SUM(perdida_tecnica_kwh) AS perdida_tecnica_kwh, SUM(perdida_no_tecnica_kwh) AS perdida_no_tecnica_kwh,
       SUM(brecha_medicion_kwh) AS brecha_medicion_kwh, SUM(perdida_total_usd) AS perdida_total_usd,
       SUM(perdida_evitable_usd) AS perdida_evitable_usd,
       SUM(perdida_total_kwh) / NULLIF(SUM(injected_kwh), 0) AS perdida_total_pct
FROM [energrid].[vw_perdida_transformador_mes]
GROUP BY zone_id, period_start;
GO

-- 4.1 Cartera por antiguedad (snapshot enriquecido)
CREATE OR ALTER VIEW [energrid].[vw_cartera_snapshot] AS
SELECT s.snapshot_date, s.invoice_id, s.client_id, c.client_type, c.tariff_id, c.zone_id, c.circuit_id, c.transformer_id,
       s.amount_due, s.balance, s.days_overdue, s.aging_bucket, s.aging_order,
       CAST(CASE WHEN s.days_overdue > 60 THEN 1 ELSE 0 END AS BIT) AS es_mayor_60,
       pc.segmento_consumo, pc.patron_variabilidad
FROM [energrid].[fact_cartera_snapshot] s
JOIN [energrid].[dim_cliente] c ON c.client_id = s.client_id
LEFT JOIN [energrid].[vw_cliente_patron_consumo] pc ON pc.client_id = s.client_id;
GO

-- 4.2 Cobranza mensual (Collection Efficiency)
CREATE OR ALTER VIEW [energrid].[vw_cobranza_mes] AS
WITH p AS (SELECT periodo_desde, fecha_corte FROM [energrid].[cfg_parametros] WHERE id = 1), u AS (
    SELECT DATEFROMPARTS(YEAR(pg.payment_date), MONTH(pg.payment_date), 1) AS period_start, c.zone_id, c.tariff_id, c.client_type,
           SUM(pg.payment_amount) AS cobros_usd, CAST(0 AS DECIMAL(18,2)) AS exigible_usd, CAST(0 AS DECIMAL(18,2)) AS emitido_usd
    FROM [energrid].[fact_pago] pg JOIN [energrid].[dim_cliente] c ON c.client_id = pg.client_id CROSS JOIN p
    WHERE pg.payment_date >= p.periodo_desde AND pg.payment_date <= p.fecha_corte
    GROUP BY DATEFROMPARTS(YEAR(pg.payment_date), MONTH(pg.payment_date), 1), c.zone_id, c.tariff_id, c.client_type
    UNION ALL
    SELECT DATEFROMPARTS(YEAR(d.due_date), MONTH(d.due_date), 1), d.zone_id, d.tariff_id, d.client_type, 0, SUM(d.amount_due), 0
    FROM [energrid].[vw_facturacion_detalle] d CROSS JOIN p WHERE d.due_date <= p.fecha_corte
    GROUP BY DATEFROMPARTS(YEAR(d.due_date), MONTH(d.due_date), 1), d.zone_id, d.tariff_id, d.client_type
    UNION ALL
    SELECT DATEFROMPARTS(YEAR(d.invoice_date), MONTH(d.invoice_date), 1), d.zone_id, d.tariff_id, d.client_type, 0, 0, SUM(d.amount_due)
    FROM [energrid].[vw_facturacion_detalle] d CROSS JOIN p WHERE d.invoice_date <= p.fecha_corte
    GROUP BY DATEFROMPARTS(YEAR(d.invoice_date), MONTH(d.invoice_date), 1), d.zone_id, d.tariff_id, d.client_type
)
SELECT period_start, zone_id, tariff_id, client_type, SUM(cobros_usd) AS cobros_usd,
       SUM(exigible_usd) AS exigible_usd, SUM(emitido_usd) AS emitido_usd
FROM u GROUP BY period_start, zone_id, tariff_id, client_type;
GO

-- 4.3 DSO y morosidad > 60 dias por zona y mes
CREATE OR ALTER VIEW [energrid].[vw_dso_zona_mes] AS
WITH p AS (SELECT periodo_desde FROM [energrid].[cfg_parametros] WHERE id = 1),
sm AS (
    SELECT s.snapshot_date, c.zone_id, SUM(s.balance) AS saldo_total,
           SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) AS saldo_mayor_60
    FROM [energrid].[fact_cartera_snapshot] s JOIN [energrid].[dim_cliente] c ON c.client_id = s.client_id
    GROUP BY s.snapshot_date, c.zone_id
), fm AS (
    SELECT dts.snapshot_date, f.zone_id, SUM(f.amount_due) AS facturado_ventana
    FROM (SELECT DISTINCT snapshot_date FROM [energrid].[fact_cartera_snapshot]) dts
    JOIN [energrid].[vw_facturacion_detalle] f
      ON f.invoice_date > DATEADD(DAY, -90, dts.snapshot_date) AND f.invoice_date <= dts.snapshot_date
    GROUP BY dts.snapshot_date, f.zone_id
)
SELECT sm.snapshot_date, sm.zone_id, sm.saldo_total, sm.saldo_mayor_60,
       sm.saldo_mayor_60 / NULLIF(sm.saldo_total, 0) AS pct_cartera_mayor_60,
       ISNULL(fm.facturado_ventana, 0) AS facturado_ventana,
       CASE WHEN DATEDIFF(DAY, p.periodo_desde, sm.snapshot_date) + 1 < 90
            THEN DATEDIFF(DAY, p.periodo_desde, sm.snapshot_date) + 1 ELSE 90 END AS dias_ventana,
       sm.saldo_total / NULLIF(fm.facturado_ventana /
           CASE WHEN DATEDIFF(DAY, p.periodo_desde, sm.snapshot_date) + 1 < 90
                THEN DATEDIFF(DAY, p.periodo_desde, sm.snapshot_date) + 1 ELSE 90 END, 0) AS dso_dias
FROM sm CROSS JOIN p LEFT JOIN fm ON fm.snapshot_date = sm.snapshot_date AND fm.zone_id = sm.zone_id;
GO

-- 5.1 Disponibilidad AMI por mes
CREATE OR ALTER VIEW [energrid].[vw_ami_mes] AS
SELECT DATEFROMPARTS(l.period_year, l.period_month, 1) AS period_start, c.zone_id, c.circuit_id, c.transformer_id,
       COUNT(*) AS lecturas_esperadas,
       SUM(CASE WHEN l.ami_valid = 1 AND l.reading_status = N'Leído' THEN 1 ELSE 0 END) AS lecturas_validas
FROM [energrid].[fact_lectura_energia] l
JOIN [energrid].[dim_medidor] m ON m.meter_id = l.meter_id AND m.ami_enabled = 1
JOIN [energrid].[dim_cliente] c ON c.client_id = l.client_id
CROSS JOIN [energrid].[cfg_parametros] p
WHERE DATEFROMPARTS(l.period_year, l.period_month, 1) BETWEEN DATEFROMPARTS(YEAR(p.periodo_desde), MONTH(p.periodo_desde), 1) AND p.periodo_hasta AND p.id = 1
GROUP BY DATEFROMPARTS(l.period_year, l.period_month, 1), c.zone_id, c.circuit_id, c.transformer_id;
GO

-- 5.2 Consumo estimado por mes
CREATE OR ALTER VIEW [energrid].[vw_consumo_estimado_mes] AS
SELECT DATEFROMPARTS(d.period_year, d.period_month, 1) AS period_start, d.zone_id, d.circuit_id, d.transformer_id,
       d.tariff_id, d.client_type, COUNT(*) AS facturas_emitidas,
       SUM(CASE WHEN d.is_estimated = 1 THEN 1 ELSE 0 END) AS facturas_estimadas,
       SUM(CASE WHEN d.reading_status = N'Faltante' THEN 1 ELSE 0 END) AS facturas_lectura_faltante,
       SUM(CASE WHEN d.is_estimated = 1 THEN d.billed_kwh ELSE 0 END) AS kwh_estimados
FROM [energrid].[vw_facturacion_detalle] d
GROUP BY DATEFROMPARTS(d.period_year, d.period_month, 1), d.zone_id, d.circuit_id, d.transformer_id, d.tariff_id, d.client_type;
GO

-- 6.1 Detalle de interrupciones de servicio
CREATE OR ALTER VIEW [energrid].[vw_interrupciones_detalle] AS
SELECT i.outage_id, i.start_datetime, i.end_datetime, DATEFROMPARTS(YEAR(i.start_datetime), MONTH(i.start_datetime), 1) AS period_start,
       i.zone_id, i.circuit_id, i.transformer_id, i.cause_id, ca.cause_name, CAST(ISNULL(i.planned, 0) AS BIT) AS planned,
       i.duration_minutes, i.customers_affected,
       CAST(i.duration_minutes AS BIGINT) * i.customers_affected AS cliente_minutos, i.latitude, i.longitude
FROM [energrid].[fact_interrupcion] i
JOIN [energrid].[dim_causa] ca ON ca.cause_id = i.cause_id
CROSS JOIN [energrid].[cfg_parametros] p
WHERE p.id = 1 AND i.start_datetime >= p.periodo_desde AND i.start_datetime < DATEADD(DAY, 1, p.periodo_hasta)
  AND (p.incluir_planificadas = 1 OR ISNULL(i.planned, 0) = 0);
GO

-- 6.2 Confiabilidad SAIDI / SAIFI / MTTR por zona y mes
CREATE OR ALTER VIEW [energrid].[vw_saidi_saifi_zona_mes] AS
WITH ev AS (SELECT period_start, zone_id, COUNT(*) AS eventos, SUM(customers_affected) AS clientes_interrumpidos,
                   SUM(cliente_minutos) AS cliente_minutos, SUM(duration_minutes) AS minutos
            FROM [energrid].[vw_interrupciones_detalle] GROUP BY period_start, zone_id),
cs AS (SELECT zone_id, SUM(clientes_servidos) AS clientes_servidos FROM [energrid].[vw_clientes_servidos] GROUP BY zone_id)
SELECT ev.*, cs.clientes_servidos,
       CAST(ev.cliente_minutos AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saidi_min,
       CAST(ev.clientes_interrumpidos AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saifi,
       CAST(ev.minutos AS FLOAT) / NULLIF(ev.eventos, 0) AS mttr_min
FROM ev LEFT JOIN cs ON cs.zone_id = ev.zone_id;
GO

-- 6.3 SAIDI / SAIFI por circuito y causa
CREATE OR ALTER VIEW [energrid].[vw_saidi_saifi_circuito_causa] AS
WITH ev AS (SELECT circuit_id, zone_id, cause_id, cause_name, planned, COUNT(*) AS eventos,
                   SUM(customers_affected) AS clientes_interrumpidos, SUM(cliente_minutos) AS cliente_minutos, SUM(duration_minutes) AS minutos
            FROM [energrid].[vw_interrupciones_detalle] GROUP BY circuit_id, zone_id, cause_id, cause_name, planned),
cs AS (SELECT circuit_id, SUM(clientes_servidos) AS clientes_servidos FROM [energrid].[vw_clientes_servidos] GROUP BY circuit_id)
SELECT ev.*, cs.clientes_servidos,
       CAST(ev.cliente_minutos AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saidi_min,
       CAST(ev.clientes_interrumpidos AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saifi,
       CAST(ev.minutos AS FLOAT) / NULLIF(ev.eventos, 0) AS mttr_min
FROM ev LEFT JOIN cs ON cs.circuit_id = ev.circuit_id;
GO

-- 6.4 Circuitos recurrentes criticos (eventos >= P90 y >= 6 meses)
CREATE OR ALTER VIEW [energrid].[vw_circuitos_recurrentes] AS
WITH c AS (SELECT circuit_id, COUNT(*) AS eventos, COUNT(DISTINCT period_start) AS meses_con_evento,
                  SUM(cliente_minutos) AS cliente_minutos
           FROM [energrid].[vw_interrupciones_detalle] WHERE circuit_id > 0 GROUP BY circuit_id),
cc AS (SELECT circuit_id, cause_name, ROW_NUMBER() OVER (PARTITION BY circuit_id ORDER BY COUNT(*) DESC, cause_name) AS rn
       FROM [energrid].[vw_interrupciones_detalle] WHERE circuit_id > 0 GROUP BY circuit_id, cause_name),
pc AS (SELECT c.*, PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY eventos) OVER () AS p90_eventos FROM c)
SELECT pc.circuit_id, pc.eventos, pc.meses_con_evento, pc.cliente_minutos, pc.p90_eventos, cc.cause_name AS causa_principal,
       CAST(CASE WHEN pc.eventos >= pc.p90_eventos AND pc.meses_con_evento >= 6 THEN 1 ELSE 0 END AS BIT) AS es_recurrente
FROM pc JOIN cc ON cc.circuit_id = pc.circuit_id AND cc.rn = 1;
GO

-- 7.1 Indice de criticidad por zona
CREATE OR ALTER VIEW [energrid].[vw_criticidad_zona] AS
WITH p AS (SELECT * FROM [energrid].[cfg_parametros] WHERE id = 1),
l AS (SELECT zone_id, SUM(injected_kwh) AS injected_kwh, SUM(perdida_total_kwh) AS perdida_kwh,
             SUM(perdida_evitable_usd) AS impacto_usd FROM [energrid].[vw_perdida_transformador_mes] GROUP BY zone_id),
m AS (SELECT zone_id, saldo_total, saldo_mayor_60 FROM [energrid].[vw_dso_zona_mes] d CROSS JOIN p WHERE d.snapshot_date = p.fecha_corte),
q AS (SELECT zone_id, SUM(cliente_minutos) AS cm, SUM(clientes_interrumpidos) AS ci, MAX(clientes_servidos) AS cs
      FROM [energrid].[vw_saidi_saifi_zona_mes] GROUP BY zone_id),
raw AS (
    SELECT z.zone_id, z.zone_name, z.region, l.impacto_usd, l.perdida_kwh / NULLIF(l.injected_kwh, 0) AS perdida_pct,
           m.saldo_mayor_60 / NULLIF(m.saldo_total, 0) AS morosidad_pct, m.saldo_mayor_60 AS cartera_mayor_60_usd,
           CAST(q.cm AS FLOAT) / NULLIF(q.cs, 0) AS saidi_min, CAST(q.ci AS FLOAT) / NULLIF(q.cs, 0) AS saifi
    FROM [energrid].[dim_zona] z JOIN l ON l.zone_id = z.zone_id LEFT JOIN m ON m.zone_id = z.zone_id LEFT JOIN q ON q.zone_id = z.zone_id
    WHERE z.zone_id > 0
), nrm AS (
    SELECT raw.*,
      100.0 * (impacto_usd - MIN(impacto_usd) OVER ()) / NULLIF(MAX(impacto_usd) OVER () - MIN(impacto_usd) OVER (), 0) AS norm_impacto,
      100.0 * (perdida_pct - MIN(perdida_pct) OVER ()) / NULLIF(MAX(perdida_pct) OVER () - MIN(perdida_pct) OVER (), 0) AS norm_perdida,
      100.0 * (morosidad_pct - MIN(morosidad_pct) OVER ()) / NULLIF(MAX(morosidad_pct) OVER () - MIN(morosidad_pct) OVER (), 0) AS norm_morosidad,
      100.0 * (saidi_min - MIN(saidi_min) OVER ()) / NULLIF(MAX(saidi_min) OVER () - MIN(saidi_min) OVER (), 0) AS norm_saidi,
      100.0 * (saifi - MIN(saifi) OVER ()) / NULLIF(MAX(saifi) OVER () - MIN(saifi) OVER (), 0) AS norm_saifi
    FROM raw
), sc AS (
    SELECT nrm.*, p.w_saidi_en_calidad * ISNULL(norm_saidi, 0) + (1 - p.w_saidi_en_calidad) * ISNULL(norm_saifi, 0) AS norm_calidad
    FROM nrm CROSS JOIN p
)
SELECT sc.*, p.w_impacto * ISNULL(norm_impacto, 0) + p.w_perdida * ISNULL(norm_perdida, 0)
            + p.w_morosidad * ISNULL(norm_morosidad, 0) + p.w_calidad * norm_calidad AS score_criticidad,
       RANK() OVER (ORDER BY p.w_impacto * ISNULL(norm_impacto, 0) + p.w_perdida * ISNULL(norm_perdida, 0)
            + p.w_morosidad * ISNULL(norm_morosidad, 0) + p.w_calidad * norm_calidad DESC) AS ranking
FROM sc CROSS JOIN p;
GO

-- 7.2 Indice de criticidad por transformador
CREATE OR ALTER VIEW [energrid].[vw_criticidad_transformador] AS
WITH p AS (SELECT * FROM [energrid].[cfg_parametros] WHERE id = 1),
l AS (SELECT transformer_id, SUM(injected_kwh) AS injected_kwh, SUM(perdida_total_kwh) AS perdida_kwh,
             SUM(perdida_evitable_usd) AS impacto_usd FROM [energrid].[vw_perdida_transformador_mes]
      WHERE transformer_id > 0 GROUP BY transformer_id),
m AS (SELECT c.transformer_id, SUM(s.balance) AS saldo_total, SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) AS saldo_mayor_60
      FROM [energrid].[fact_cartera_snapshot] s JOIN [energrid].[dim_cliente] c ON c.client_id = s.client_id CROSS JOIN p
      WHERE s.snapshot_date = p.fecha_corte GROUP BY c.transformer_id),
cs AS (SELECT transformer_id, SUM(clientes_servidos) AS clientes_servidos FROM [energrid].[vw_clientes_servidos] GROUP BY transformer_id),
q AS (SELECT transformer_id, COUNT(*) AS eventos, SUM(cliente_minutos) AS cm, SUM(customers_affected) AS ci
      FROM [energrid].[vw_interrupciones_detalle] GROUP BY transformer_id),
raw AS (
    SELECT t.transformer_id, t.transformer_name, t.circuit_id, t.zone_id, t.capacity_kva, t.latitude, t.longitude, cs.clientes_servidos,
           l.impacto_usd, l.perdida_kwh / NULLIF(l.injected_kwh, 0) AS perdida_pct,
           m.saldo_mayor_60 / NULLIF(m.saldo_total, 0) AS morosidad_pct, m.saldo_mayor_60 AS cartera_mayor_60_usd,
           ISNULL(q.eventos, 0) AS eventos, ISNULL(q.cm, 0) AS cliente_minutos,
           CAST(ISNULL(q.cm, 0) AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saidi_min,
           CAST(ISNULL(q.ci, 0) AS FLOAT) / NULLIF(cs.clientes_servidos, 0) AS saifi
    FROM [energrid].[dim_transformador] t JOIN l ON l.transformer_id = t.transformer_id
    LEFT JOIN m ON m.transformer_id = t.transformer_id LEFT JOIN cs ON cs.transformer_id = t.transformer_id
    LEFT JOIN q ON q.transformer_id = t.transformer_id WHERE t.transformer_id > 0
), nrm AS (
    SELECT raw.*,
      100.0 * (impacto_usd - MIN(impacto_usd) OVER ()) / NULLIF(MAX(impacto_usd) OVER () - MIN(impacto_usd) OVER (), 0) AS norm_impacto,
      100.0 * (perdida_pct - MIN(perdida_pct) OVER ()) / NULLIF(MAX(perdida_pct) OVER () - MIN(perdida_pct) OVER (), 0) AS norm_perdida,
      100.0 * (morosidad_pct - MIN(morosidad_pct) OVER ()) / NULLIF(MAX(morosidad_pct) OVER () - MIN(morosidad_pct) OVER (), 0) AS norm_morosidad,
      100.0 * (saidi_min - MIN(saidi_min) OVER ()) / NULLIF(MAX(saidi_min) OVER () - MIN(saidi_min) OVER (), 0) AS norm_saidi,
      100.0 * (saifi - MIN(saifi) OVER ()) / NULLIF(MAX(saifi) OVER () - MIN(saifi) OVER (), 0) AS norm_saifi
    FROM raw
), sc AS (
    SELECT nrm.*, p.w_saidi_en_calidad * ISNULL(norm_saidi, 0) + (1 - p.w_saidi_en_calidad) * ISNULL(norm_saifi, 0) AS norm_calidad
    FROM nrm CROSS JOIN p
), fin AS (
    SELECT sc.*, p.w_impacto * ISNULL(norm_impacto, 0) + p.w_perdida * ISNULL(norm_perdida, 0)
               + p.w_morosidad * ISNULL(norm_morosidad, 0) + p.w_calidad * norm_calidad AS score_criticidad
    FROM sc CROSS JOIN p
)
SELECT fin.*, RANK() OVER (ORDER BY score_criticidad DESC) AS ranking
FROM fin;
GO

-- 7.3 Top 10 transformadores a intervenir
CREATE OR ALTER VIEW [energrid].[vw_top10_transformadores] AS
SELECT TOP (10) c.ranking, c.transformer_id, c.transformer_name, c.circuit_id, c.zone_id, c.latitude, c.longitude, c.score_criticidad,
       c.impacto_usd AS beneficio_usd_recuperable,
       CAST(c.cliente_minutos AS FLOAT) / NULLIF((SELECT SUM(clientes_servidos) FROM [energrid].[vw_clientes_servidos]), 0) AS saidi_min_evitables_sistema,
       c.perdida_pct, c.morosidad_pct, c.saidi_min, c.saifi, c.cartera_mayor_60_usd, d.componente_dominante,
       CASE d.componente_dominante WHEN N'Impacto económico' THEN N'USD de pérdida evitable mensual'
            WHEN N'Pérdida relativa' THEN N'% pérdida total mensual del transformador'
            WHEN N'Morosidad' THEN N'% cartera > 60 días'
            ELSE N'SAIDI / SAIFI del transformador' END AS kpi_seguimiento
FROM [energrid].[vw_criticidad_transformador] c CROSS JOIN [energrid].[cfg_parametros] p
CROSS APPLY (SELECT TOP (1) comp AS componente_dominante FROM (VALUES
        (N'Impacto económico', p.w_impacto   * ISNULL(c.norm_impacto, 0)),
        (N'Pérdida relativa',  p.w_perdida   * ISNULL(c.norm_perdida, 0)),
        (N'Morosidad',         p.w_morosidad * ISNULL(c.norm_morosidad, 0)),
        (N'Calidad de servicio', p.w_calidad * c.norm_calidad)) v(comp, aporte) ORDER BY aporte DESC) d
WHERE p.id = 1
ORDER BY c.ranking;
GO

-- 8. Alertas del sistema (perdida anomala, deterioro SAIDI, disponibilidad AMI baja)
CREATE OR ALTER VIEW [energrid].[vw_alertas] AS
WITH p AS (SELECT * FROM [energrid].[cfg_parametros] WHERE id = 1),
c AS (
    SELECT circuit_id, MAX(zone_id) AS zone_id, period_start,
           SUM(perdida_total_kwh) / NULLIF(SUM(injected_kwh), 0) AS perdida_pct
    FROM [energrid].[vw_perdida_transformador_mes] WHERE circuit_id > 0
    GROUP BY circuit_id, period_start HAVING SUM(injected_kwh) > 0
), a1 AS (
    SELECT N'Pérdida anómala' AS tipo_alerta, N'Circuito' AS nivel,
           x.circuit_id AS entidad_id, x.zone_id, x.period_start,
           x.perdida_pct AS valor, x.z AS indicador, p.alerta_zscore_perdida AS umbral,
           CASE WHEN COUNT(*) OVER (PARTITION BY x.circuit_id) >= 6 THEN N'Alta' ELSE N'Media' END AS severidad,
           COUNT(*) OVER (PARTITION BY x.circuit_id) AS meses_en_alerta
    FROM (SELECT c.*, (c.perdida_pct - AVG(c.perdida_pct) OVER (PARTITION BY c.period_start))
                      / NULLIF(STDEV(c.perdida_pct) OVER (PARTITION BY c.period_start), 0) AS z FROM c) x
    CROSS JOIN p WHERE x.z > p.alerta_zscore_perdida
), s AS (
    SELECT zone_id, period_start, saidi_min,
           AVG(saidi_min) OVER (PARTITION BY zone_id ORDER BY period_start
                                ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS saidi_previo
    FROM [energrid].[vw_saidi_saifi_zona_mes]
), a2 AS (
    SELECT N'Deterioro SAIDI' AS tipo_alerta, N'Zona' AS nivel,
           s.zone_id AS entidad_id, s.zone_id, s.period_start,
           s.saidi_min AS valor,
           s.saidi_min / NULLIF(s.saidi_previo, 0) - 1 AS indicador,
           p.alerta_saidi_deterioro AS umbral,
           CASE WHEN s.saidi_min > s.saidi_previo * (1 + 2 * p.alerta_saidi_deterioro)
                THEN N'Alta' ELSE N'Media' END AS severidad,
           COUNT(*) OVER (PARTITION BY s.zone_id) AS meses_en_alerta
    FROM s CROSS JOIN p
    WHERE s.saidi_previo IS NOT NULL
      AND s.saidi_min > s.saidi_previo * (1 + p.alerta_saidi_deterioro)
), am AS (
    SELECT zone_id, period_start,
           SUM(lecturas_validas) * 1.0 / NULLIF(SUM(lecturas_esperadas), 0) AS disponibilidad
    FROM [energrid].[vw_ami_mes] GROUP BY zone_id, period_start
), a3 AS (
    SELECT N'Disponibilidad AMI baja' AS tipo_alerta, N'Zona' AS nivel,
           am.zone_id AS entidad_id, am.zone_id, am.period_start,
           am.disponibilidad AS valor,
           CAST(NULL AS FLOAT) AS indicador,
           p.alerta_ami_min AS umbral,
           CASE WHEN am.disponibilidad < p.alerta_ami_min - 0.02 THEN N'Alta' ELSE N'Media' END AS severidad,
           COUNT(*) OVER (PARTITION BY am.zone_id) AS meses_en_alerta
    FROM am CROSS JOIN p
    WHERE am.disponibilidad < p.alerta_ami_min
)
SELECT * FROM a1
UNION ALL SELECT * FROM a2
UNION ALL SELECT * FROM a3;
GO

-- 9.1 Resumen ejecutivo de KPIs
CREATE OR ALTER VIEW [energrid].[vw_kpi_resumen] AS
WITH p AS (SELECT * FROM [energrid].[cfg_parametros] WHERE id = 1),
cs AS (SELECT SUM(clientes_servidos) AS n FROM [energrid].[vw_clientes_servidos]),
cart AS (SELECT SUM(balance) AS saldo, SUM(CASE WHEN days_overdue > 60 THEN balance ELSE 0 END) AS saldo60
         FROM [energrid].[fact_cartera_snapshot] s CROSS JOIN p WHERE s.snapshot_date = p.fecha_corte),
dso AS (SELECT SUM(saldo_total) / NULLIF(SUM(facturado_ventana) / MAX(dias_ventana), 0) AS dso
        FROM [energrid].[vw_dso_zona_mes] d CROSS JOIN p WHERE d.snapshot_date = p.fecha_corte)
SELECT
  (SELECT SUM(perdida_total_kwh) / NULLIF(SUM(injected_kwh), 0) FROM [energrid].[vw_perdida_transformador_mes])           AS perdida_energia_pct,
  (SELECT SUM(c) / NULLIF(SUM(e), 0) FROM (SELECT SUM(cobros_usd) AS c, SUM(exigible_usd) AS e
                                           FROM [energrid].[vw_cobranza_mes] GROUP BY period_start HAVING SUM(exigible_usd) > 0) t) AS collection_efficiency,
  cart.saldo60                                                                                                          AS cartera_mayor_60_usd,
  cart.saldo60 / NULLIF(cart.saldo, 0)                                                                                  AS cartera_mayor_60_pct,
  dso.dso                                                                                                               AS dso_dias,
  (SELECT SUM(lecturas_validas) * 1.0 / NULLIF(SUM(lecturas_esperadas), 0) FROM [energrid].[vw_ami_mes])                AS disponibilidad_ami,
  (SELECT SUM(facturas_estimadas) * 1.0 / NULLIF(SUM(facturas_emitidas), 0) FROM [energrid].[vw_consumo_estimado_mes])  AS consumo_estimado_pct,
  (SELECT SUM(cliente_minutos) * 1.0 FROM [energrid].[vw_interrupciones_detalle]) / NULLIF(cs.n, 0)                     AS saidi_min,
  (SELECT SUM(customers_affected) * 1.0 FROM [energrid].[vw_interrupciones_detalle]) / NULLIF(cs.n, 0)                  AS saifi,
  (SELECT AVG(duration_minutes * 1.0) FROM [energrid].[vw_interrupciones_detalle])                                      AS mttr_min,
  (SELECT MAX(score_criticidad) FROM [energrid].[vw_criticidad_zona])                                                   AS criticidad_max_zona
FROM cs CROSS JOIN cart CROSS JOIN dso;
GO

-- 9.2 Matriz de control y reconciliacion
CREATE OR ALTER VIEW [energrid].[vw_control_reconciliacion] AS
WITH p AS (SELECT * FROM [energrid].[cfg_parametros] WHERE id = 1),
chk AS (
    SELECT N'Energía inyectada: vista = hecho diario' AS control,
           (SELECT SUM(f.injected_kwh) FROM [energrid].[fact_energia_transformador_diaria] f CROSS JOIN p
             WHERE f.[date] BETWEEN p.periodo_desde AND p.periodo_hasta) AS esperado,
           (SELECT SUM(injected_kwh) FROM [energrid].[vw_perdida_transformador_mes]) AS obtenido, 1.0 AS tolerancia
    UNION ALL
    SELECT N'kWh facturados: vista = hecho de facturación (sin doble conteo)',
           (SELECT SUM(fa.billed_kwh) FROM [energrid].[fact_facturacion] fa CROSS JOIN p
             WHERE DATEFROMPARTS(fa.period_year, fa.period_month, 1) BETWEEN DATEFROMPARTS(YEAR(p.periodo_desde), MONTH(p.periodo_desde), 1) AND p.periodo_hasta),
           (SELECT SUM(billed_kwh) FROM [energrid].[vw_perdida_transformador_mes]), 1.0
    UNION ALL
    SELECT N'Identidad: pérdida total = técnica + no técnica + brecha de medición', 0,
           (SELECT SUM(perdida_total_kwh - perdida_tecnica_kwh - perdida_no_tecnica_kwh - brecha_medicion_kwh) FROM [energrid].[vw_perdida_transformador_mes]), 1.0
    UNION ALL
    SELECT N'Suma de zonas = total del sistema (kWh perdidos)',
           (SELECT SUM(perdida_total_kwh) FROM [energrid].[vw_perdida_transformador_mes]),
           (SELECT SUM(perdida_total_kwh) FROM [energrid].[vw_perdida_zona_mes]), 1.0
    UNION ALL
    SELECT N'Cartera al corte = facturado - pagos (facturas con saldo > 0)',
           (SELECT SUM(CASE WHEN x.s > 0 THEN x.s ELSE 0 END) FROM (SELECT f.amount_due - ISNULL(SUM(pg.payment_amount), 0) AS s
               FROM [energrid].[fact_facturacion] f CROSS JOIN p
               LEFT JOIN [energrid].[fact_pago] pg ON pg.invoice_id = f.invoice_id AND pg.payment_date <= p.fecha_corte
               WHERE f.invoice_date <= p.fecha_corte GROUP BY f.invoice_id, f.amount_due) x),
           (SELECT SUM(balance) FROM [energrid].[fact_cartera_snapshot] s CROSS JOIN p WHERE s.snapshot_date = p.fecha_corte), 1.0
    UNION ALL
    SELECT N'Clientes interrumpidos (SAIFI): vista de zona = detalle',
           (SELECT SUM(customers_affected) FROM [energrid].[vw_interrupciones_detalle]),
           (SELECT SUM(clientes_interrumpidos) FROM [energrid].[vw_saidi_saifi_zona_mes]), 0.0
)
SELECT control, esperado, obtenido, CAST(CASE WHEN ABS(ISNULL(esperado, 0) - ISNULL(obtenido, 0)) <= tolerancia THEN 1 ELSE 0 END AS BIT) AS ok
FROM chk;
GO
