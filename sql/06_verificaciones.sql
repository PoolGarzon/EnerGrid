-- 06_verificaciones.sql: Consultas de verificacion y consistencia (solo lectura)
USE EnerGrid;
GO

-- 1. Conciliacion y KPIs del modelo
SELECT * FROM energrid.vw_control_reconciliacion;
GO
SELECT * FROM energrid.vw_kpi_resumen;
GO
SELECT * FROM energrid.vw_criticidad_zona ORDER BY ranking;
GO
SELECT * FROM energrid.vw_top10_transformadores;
GO

-- 2. Estado del refresco y SLA
SELECT * FROM energrid.cfg_sla_fuente ORDER BY orden;
GO
SELECT TOP 20 * FROM energrid.aud_refresco_log ORDER BY run_id DESC;
GO
SELECT * FROM energrid.mat_fuente_estado;
GO
SELECT * FROM energrid.mat_resumen_estado;
GO
SELECT name, create_date, modify_date FROM sys.tables
 WHERE schema_id = SCHEMA_ID(N'energrid') AND name IN (N'mat_perdida_circuito_mes', N'mat_cartera_dim', N'mat_facturado_dim', N'mat_cobranza_dim_mes', N'mat_ami_dim_mes')
 ORDER BY name;
GO

-- 3. Coherencia entre tablas de resumen mat_*
SELECT * FROM energrid.mat_control_reconciliacion;
GO
SELECT 'perdida kWh' chk, (SELECT SUM(perdida_total_kwh) FROM energrid.mat_perdida_zona_mes) a, (SELECT SUM(perdida_total_kwh) FROM energrid.mat_perdida_circuito_mes) b
UNION ALL
SELECT 'cartera USD', (SELECT SUM(saldo) FROM energrid.mat_cartera_resumen), (SELECT SUM(saldo) FROM energrid.mat_cartera_dim)
UNION ALL
SELECT 'cobros', (SELECT SUM(cobros_usd) FROM energrid.mat_cobranza_mes), (SELECT SUM(cobros_usd) FROM energrid.mat_cobranza_dim_mes)
UNION ALL
SELECT 'exigible', (SELECT SUM(exigible_usd) FROM energrid.mat_cobranza_mes), (SELECT SUM(exigible_usd) FROM energrid.mat_cobranza_dim_mes)
UNION ALL
SELECT 'AMI esperadas', (SELECT SUM(CAST(lecturas_esperadas AS bigint)) FROM energrid.mat_ami_mes), (SELECT SUM(CAST(lecturas_esperadas AS bigint)) FROM energrid.mat_ami_dim_mes);
GO
SELECT SUM(c)/SUM(e) ce FROM (SELECT SUM(cobros_usd) c, SUM(exigible_usd) e FROM energrid.mat_cobranza_mes GROUP BY period_start HAVING SUM(exigible_usd) > 0) t;
GO

-- 4. Integridad: lecturas por medidor AMI y mes
WITH x AS (
  SELECT l.period_year AS y, l.period_month AS m, l.meter_id, COUNT(*) AS lect
  FROM energrid.fact_lectura_energia l
  JOIN energrid.dim_medidor md ON md.meter_id = l.meter_id AND md.ami_enabled = 1
  GROUP BY l.period_year, l.period_month, l.meter_id)
SELECT y AS anio, m AS mes, COUNT(*) AS medidores_con_lectura, SUM(lect) AS lecturas,
       MAX(lect) AS max_lect_por_medidor, SUM(CASE WHEN lect > 1 THEN 1 ELSE 0 END) AS medidores_con_mas_de_1
FROM x GROUP BY y, m ORDER BY y, m;
GO

-- 5. Disponibilidad AMI mensual: formulas F1 vs F2 vs F3
WITH base AS (
  SELECT DATEFROMPARTS(l.period_year, l.period_month, 1) AS period_start, l.meter_id, m.status AS meter_status,
         CASE WHEN l.ami_valid = 1 AND l.reading_status = N'Leído' THEN 1 ELSE 0 END AS valida
  FROM energrid.fact_lectura_energia l
  JOIN energrid.dim_medidor m ON m.meter_id = l.meter_id AND m.ami_enabled = 1
  JOIN energrid.dim_cliente c ON c.client_id = l.client_id
  WHERE c.zone_id > 0),
snap AS (SELECT COUNT(*) AS activos_snapshot FROM energrid.dim_medidor WHERE ami_enabled = 1 AND status = N'Activo')
SELECT b.period_start, SUM(b.valida) AS lecturas_validas, COUNT(*) AS lecturas_esperadas,
       CAST(SUM(b.valida) * 1.0 / COUNT(*) AS decimal(7,5)) AS f1_actual,
       CAST(COUNT(DISTINCT CASE WHEN b.valida = 1 THEN b.meter_id END) * 1.0 / COUNT(DISTINCT b.meter_id) AS decimal(7,5)) AS f2_caso,
       CAST(SUM(CASE WHEN b.meter_status = N'Activo' THEN b.valida ELSE 0 END) * 1.0 / MAX(s.activos_snapshot) AS decimal(7,5)) AS f3_snapshot
FROM base b CROSS JOIN snap s
GROUP BY b.period_start ORDER BY b.period_start;
GO

-- 6. Disponibilidad AMI por mes y zona
DECLARE @zona int = 4, @mes date = '2025-01-01';
WITH base AS (
  SELECT DATEFROMPARTS(l.period_year, l.period_month, 1) AS period_start, c.zone_id, l.meter_id, m.status AS meter_status,
         CASE WHEN l.ami_valid = 1 AND l.reading_status = N'Leído' THEN 1 ELSE 0 END AS valida
  FROM energrid.fact_lectura_energia l
  JOIN energrid.dim_medidor m ON m.meter_id = l.meter_id AND m.ami_enabled = 1
  JOIN energrid.dim_cliente c ON c.client_id = l.client_id
  WHERE c.zone_id > 0),
snap AS (
  SELECT c.zone_id, COUNT(DISTINCT m.meter_id) AS activos_snapshot
  FROM energrid.dim_medidor m JOIN energrid.dim_cliente c ON c.client_id = m.client_id
  WHERE m.ami_enabled = 1 AND m.status = N'Activo' GROUP BY c.zone_id),
z AS (
  SELECT period_start, zone_id, SUM(valida) AS lecturas_validas, COUNT(*) AS lecturas_esperadas,
         COUNT(DISTINCT CASE WHEN valida = 1 THEN meter_id END) AS medidores_con_lectura_valida,
         COUNT(DISTINCT meter_id) AS medidores_activos_del_mes,
         SUM(CASE WHEN meter_status = N'Activo' THEN valida ELSE 0 END) AS validas_status_activo
  FROM base GROUP BY period_start, zone_id)
SELECT z.period_start, z.zone_id, zn.zone_name, z.lecturas_validas, z.lecturas_esperadas,
       CAST(z.lecturas_validas * 1.0 / z.lecturas_esperadas AS decimal(7,5)) AS f1_actual,
       z.medidores_con_lectura_valida, z.medidores_activos_del_mes,
       CAST(z.medidores_con_lectura_valida * 1.0 / z.medidores_activos_del_mes AS decimal(7,5)) AS f2_caso,
       CAST(z.lecturas_validas * 1.0 / z.lecturas_esperadas - z.medidores_con_lectura_valida * 1.0 / z.medidores_activos_del_mes AS decimal(9,6)) AS dif_f1_f2,
       s.activos_snapshot, z.validas_status_activo,
       CAST(z.validas_status_activo * 1.0 / s.activos_snapshot AS decimal(7,5)) AS f3_snapshot,
       p.alerta_ami_min,
       CASE WHEN z.lecturas_validas * 1.0 / z.lecturas_esperadas < p.alerta_ami_min THEN 1 ELSE 0 END AS alerta_f1_f2,
       CASE WHEN z.validas_status_activo * 1.0 / s.activos_snapshot < p.alerta_ami_min THEN 1 ELSE 0 END AS alerta_f3
FROM z
JOIN snap s ON s.zone_id = z.zone_id
JOIN energrid.dim_zona zn ON zn.zone_id = z.zone_id
CROSS JOIN energrid.cfg_parametros p
WHERE p.id = 1 AND (@zona IS NULL OR z.zone_id = @zona) AND (@mes IS NULL OR z.period_start = @mes)
ORDER BY z.period_start, z.zone_id;
GO

-- 7. Coherencia: mat_ami_mes vs hechos y alertas AMI
WITH live AS (
  SELECT DATEFROMPARTS(l.period_year, l.period_month, 1) AS ps, c.zone_id,
         SUM(CASE WHEN l.ami_valid = 1 AND l.reading_status = N'Leído' THEN 1 ELSE 0 END) AS validas, COUNT(*) AS esperadas
  FROM energrid.fact_lectura_energia l
  JOIN energrid.dim_medidor m ON m.meter_id = l.meter_id AND m.ami_enabled = 1
  JOIN energrid.dim_cliente c ON c.client_id = l.client_id
  GROUP BY DATEFROMPARTS(l.period_year, l.period_month, 1), c.zone_id),
mt AS (SELECT period_start AS ps, zone_id, SUM(lecturas_validas) AS validas, SUM(lecturas_esperadas) AS esperadas
       FROM energrid.mat_ami_mes GROUP BY period_start, zone_id),
bajo AS (SELECT zone_id, period_start AS ps FROM energrid.mat_ami_mes
         GROUP BY zone_id, period_start
         HAVING SUM(lecturas_validas) * 1.0 / NULLIF(SUM(lecturas_esperadas), 0) < (SELECT alerta_ami_min FROM energrid.cfg_parametros WHERE id = 1)),
al AS (SELECT zone_id, period_start AS ps FROM energrid.mat_alertas WHERE tipo_alerta = N'Disponibilidad AMI baja')
SELECT (SELECT COUNT(*) FROM live JOIN mt ON mt.ps = live.ps AND mt.zone_id = live.zone_id
        WHERE live.validas <> mt.validas OR live.esperadas <> mt.esperadas) AS celdas_mat_distinta_de_hechos,
       (SELECT COUNT(*) FROM bajo) AS zona_meses_bajo_umbral,
       (SELECT COUNT(*) FROM al) AS alertas_en_mat_alertas,
       (SELECT COUNT(*) FROM bajo b FULL JOIN al a ON a.zone_id = b.zone_id AND a.ps = b.ps
        WHERE b.zone_id IS NULL OR a.zone_id IS NULL) AS discrepancias_alerta;
GO
