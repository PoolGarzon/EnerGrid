-- 04_procedimientos.sql: Procedimientos de snapshot y refresco de resumenes
USE EnerGrid;
GO

-- 1. Reconstruccion del snapshot mensual de cartera
CREATE OR ALTER PROCEDURE [energrid].[usp_refresh_cartera_snapshot] AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @corte DATE, @desde DATE;
    SELECT @corte = fecha_corte, @desde = periodo_desde FROM [energrid].[cfg_parametros] WHERE id = 1;
    TRUNCATE TABLE [energrid].[fact_cartera_snapshot];
    WITH me AS (
        SELECT [date] AS snapshot_date FROM [energrid].[dim_tiempo]
        WHERE [date] = EOMONTH([date]) AND [date] BETWEEN @desde AND @corte
        UNION SELECT @corte
    )
    INSERT INTO [energrid].[fact_cartera_snapshot]
        (snapshot_date, invoice_id, client_id, amount_due, paid_amount, balance, days_overdue, aging_bucket, aging_order)
    SELECT me.snapshot_date, f.invoice_id, f.client_id, f.amount_due, ISNULL(p.paid, 0),
           f.amount_due - ISNULL(p.paid, 0), x.d,
           CASE WHEN x.d <= 0 THEN N'Vigente' WHEN x.d <= 30 THEN N'1-30' WHEN x.d <= 60 THEN N'31-60'
                WHEN x.d <= 90 THEN N'61-90' WHEN x.d <= 180 THEN N'91-180' ELSE N'>180' END,
           CASE WHEN x.d <= 0 THEN 0 WHEN x.d <= 30 THEN 1 WHEN x.d <= 60 THEN 2
                WHEN x.d <= 90 THEN 3 WHEN x.d <= 180 THEN 4 ELSE 5 END
    FROM me
    JOIN [energrid].[fact_facturacion] f ON f.invoice_date <= me.snapshot_date
    OUTER APPLY (SELECT SUM(pg.payment_amount) AS paid FROM [energrid].[fact_pago] pg
                 WHERE pg.invoice_id = f.invoice_id AND pg.payment_date <= me.snapshot_date) p
    CROSS APPLY (SELECT DATEDIFF(DAY, f.due_date, me.snapshot_date) AS d) x
    WHERE f.amount_due - ISNULL(p.paid, 0) > 0.005;
END;
GO

-- 2. Refresco atomico de resumenes mat_* por zona
CREATE OR ALTER PROCEDURE energrid.usp_refrescar_resumenes
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @t0 DATETIME2 = SYSDATETIME();

    -- Copias temporales *_new
    DROP TABLE IF EXISTS energrid.mat_perdida_zona_mes_new, energrid.mat_cobranza_mes_new, energrid.mat_ami_mes_new,
                         energrid.mat_consumo_estimado_mes_new, energrid.mat_dso_zona_mes_new, energrid.mat_cartera_resumen_new,
                         energrid.mat_criticidad_zona_new, energrid.mat_alertas_new, energrid.mat_control_reconciliacion_new,
                         energrid.mat_top10_transformadores_new;

    SELECT * INTO energrid.mat_perdida_zona_mes_new         FROM energrid.vw_perdida_zona_mes;
    SELECT * INTO energrid.mat_cobranza_mes_new             FROM energrid.vw_cobranza_mes;
    SELECT * INTO energrid.mat_ami_mes_new                  FROM energrid.vw_ami_mes;
    SELECT * INTO energrid.mat_consumo_estimado_mes_new     FROM energrid.vw_consumo_estimado_mes;
    SELECT * INTO energrid.mat_dso_zona_mes_new             FROM energrid.vw_dso_zona_mes;
    SELECT snapshot_date, zone_id, client_type, aging_bucket, aging_order, segmento_consumo,
           SUM(balance) AS saldo,
           SUM(CASE WHEN es_mayor_60 = 1 THEN balance ELSE 0 END) AS saldo_mayor_60,
           COUNT(*) AS facturas
      INTO energrid.mat_cartera_resumen_new
      FROM energrid.vw_cartera_snapshot
     GROUP BY snapshot_date, zone_id, client_type, aging_bucket, aging_order, segmento_consumo;
    SELECT * INTO energrid.mat_criticidad_zona_new          FROM energrid.vw_criticidad_zona;
    SELECT * INTO energrid.mat_alertas_new                  FROM energrid.vw_alertas;
    SELECT * INTO energrid.mat_control_reconciliacion_new   FROM energrid.vw_control_reconciliacion;
    SELECT * INTO energrid.mat_top10_transformadores_new    FROM energrid.vw_top10_transformadores;

    -- Intercambio atomico
    BEGIN TRAN;
        DROP TABLE IF EXISTS energrid.mat_perdida_zona_mes;         EXEC sp_rename N'energrid.mat_perdida_zona_mes_new',         N'mat_perdida_zona_mes';
        DROP TABLE IF EXISTS energrid.mat_cobranza_mes;             EXEC sp_rename N'energrid.mat_cobranza_mes_new',             N'mat_cobranza_mes';
        DROP TABLE IF EXISTS energrid.mat_ami_mes;                  EXEC sp_rename N'energrid.mat_ami_mes_new',                  N'mat_ami_mes';
        DROP TABLE IF EXISTS energrid.mat_consumo_estimado_mes;     EXEC sp_rename N'energrid.mat_consumo_estimado_mes_new',     N'mat_consumo_estimado_mes';
        DROP TABLE IF EXISTS energrid.mat_dso_zona_mes;             EXEC sp_rename N'energrid.mat_dso_zona_mes_new',             N'mat_dso_zona_mes';
        DROP TABLE IF EXISTS energrid.mat_cartera_resumen;          EXEC sp_rename N'energrid.mat_cartera_resumen_new',          N'mat_cartera_resumen';
        DROP TABLE IF EXISTS energrid.mat_criticidad_zona;          EXEC sp_rename N'energrid.mat_criticidad_zona_new',          N'mat_criticidad_zona';
        DROP TABLE IF EXISTS energrid.mat_alertas;                  EXEC sp_rename N'energrid.mat_alertas_new',                  N'mat_alertas';
        DROP TABLE IF EXISTS energrid.mat_control_reconciliacion;   EXEC sp_rename N'energrid.mat_control_reconciliacion_new',   N'mat_control_reconciliacion';
        DROP TABLE IF EXISTS energrid.mat_top10_transformadores;    EXEC sp_rename N'energrid.mat_top10_transformadores_new',    N'mat_top10_transformadores';

        DELETE FROM energrid.mat_resumen_estado;
        INSERT energrid.mat_resumen_estado (refrescado_en, duracion_seg) VALUES (SYSDATETIME(), DATEDIFF(SECOND, @t0, SYSDATETIME()));
    COMMIT;
END;
GO

-- 3. Refresco atomico de resumenes con dimensiones (circuito, tarifa, tipo)
CREATE OR ALTER PROCEDURE energrid.usp_refrescar_resumenes_dim
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @t0 DATETIME2 = SYSDATETIME();

    -- Copias temporales *_new
    DROP TABLE IF EXISTS energrid.mat_perdida_circuito_mes_new, energrid.mat_cartera_dim_new, energrid.mat_facturado_dim_new,
                         energrid.mat_cobranza_dim_mes_new, energrid.mat_ami_dim_mes_new;

    -- Perdida por circuito
    SELECT zone_id, circuit_id, period_start,
           SUM(injected_kwh) AS injected_kwh, SUM(delivered_kwh) AS delivered_kwh, SUM(billed_kwh) AS billed_kwh,
           SUM(perdida_total_kwh) AS perdida_total_kwh, SUM(perdida_tecnica_kwh) AS perdida_tecnica_kwh,
           SUM(perdida_no_tecnica_kwh) AS perdida_no_tecnica_kwh, SUM(brecha_medicion_kwh) AS brecha_medicion_kwh,
           SUM(perdida_total_usd) AS perdida_total_usd, SUM(perdida_evitable_usd) AS perdida_evitable_usd,
           SUM(perdida_total_kwh) / NULLIF(SUM(injected_kwh), 0) AS perdida_total_pct
      INTO energrid.mat_perdida_circuito_mes_new
      FROM energrid.vw_perdida_transformador_mes
     GROUP BY zone_id, circuit_id, period_start;

    -- Cartera multidimensional
    SELECT s.snapshot_date, c.zone_id, c.circuit_id, c.tariff_id, c.client_type,
           s.aging_bucket, s.aging_order, pc.segmento_consumo,
           SUM(s.balance) AS saldo,
           SUM(CASE WHEN s.days_overdue > 60 THEN s.balance ELSE 0 END) AS saldo_mayor_60,
           COUNT(*) AS facturas
      INTO energrid.mat_cartera_dim_new
      FROM energrid.fact_cartera_snapshot s
      JOIN energrid.dim_cliente c ON c.client_id = s.client_id
      LEFT JOIN energrid.vw_cliente_patron_consumo pc ON pc.client_id = s.client_id
     GROUP BY s.snapshot_date, c.zone_id, c.circuit_id, c.tariff_id, c.client_type,
              s.aging_bucket, s.aging_order, pc.segmento_consumo;

    -- Facturado ventana 90 dias
    SELECT dts.snapshot_date, f.zone_id, f.circuit_id, f.tariff_id, f.client_type,
           SUM(f.amount_due) AS facturado_ventana,
           MAX(CASE WHEN DATEDIFF(DAY, p.periodo_desde, dts.snapshot_date) + 1 < 90
                    THEN DATEDIFF(DAY, p.periodo_desde, dts.snapshot_date) + 1 ELSE 90 END) AS dias_ventana
      INTO energrid.mat_facturado_dim_new
      FROM (SELECT DISTINCT snapshot_date FROM energrid.fact_cartera_snapshot) dts
      JOIN energrid.vw_facturacion_detalle f
        ON f.invoice_date > DATEADD(DAY, -90, dts.snapshot_date) AND f.invoice_date <= dts.snapshot_date
      CROSS JOIN energrid.cfg_parametros p
     WHERE p.id = 1
     GROUP BY dts.snapshot_date, f.zone_id, f.circuit_id, f.tariff_id, f.client_type;

    -- Cobranza mensual por dimensiones
    ;WITH p AS (SELECT periodo_desde, fecha_corte FROM energrid.cfg_parametros WHERE id = 1), u AS (
        SELECT DATEFROMPARTS(YEAR(pg.payment_date), MONTH(pg.payment_date), 1) AS period_start, c.zone_id, c.circuit_id, c.tariff_id, c.client_type,
               SUM(pg.payment_amount) AS cobros_usd, CAST(0 AS DECIMAL(18,2)) AS exigible_usd, CAST(0 AS DECIMAL(18,2)) AS emitido_usd
          FROM energrid.fact_pago pg JOIN energrid.dim_cliente c ON c.client_id = pg.client_id CROSS JOIN p
         WHERE pg.payment_date >= p.periodo_desde AND pg.payment_date <= p.fecha_corte
         GROUP BY DATEFROMPARTS(YEAR(pg.payment_date), MONTH(pg.payment_date), 1), c.zone_id, c.circuit_id, c.tariff_id, c.client_type
        UNION ALL
        SELECT DATEFROMPARTS(YEAR(d.due_date), MONTH(d.due_date), 1), d.zone_id, d.circuit_id, d.tariff_id, d.client_type, 0, SUM(d.amount_due), 0
          FROM energrid.vw_facturacion_detalle d CROSS JOIN p WHERE d.due_date <= p.fecha_corte
         GROUP BY DATEFROMPARTS(YEAR(d.due_date), MONTH(d.due_date), 1), d.zone_id, d.circuit_id, d.tariff_id, d.client_type
        UNION ALL
        SELECT DATEFROMPARTS(YEAR(d.invoice_date), MONTH(d.invoice_date), 1), d.zone_id, d.circuit_id, d.tariff_id, d.client_type, 0, 0, SUM(d.amount_due)
          FROM energrid.vw_facturacion_detalle d CROSS JOIN p WHERE d.invoice_date <= p.fecha_corte
         GROUP BY DATEFROMPARTS(YEAR(d.invoice_date), MONTH(d.invoice_date), 1), d.zone_id, d.circuit_id, d.tariff_id, d.client_type
    )
    SELECT period_start, zone_id, circuit_id, tariff_id, client_type,
           SUM(cobros_usd) AS cobros_usd, SUM(exigible_usd) AS exigible_usd, SUM(emitido_usd) AS emitido_usd
      INTO energrid.mat_cobranza_dim_mes_new
      FROM u GROUP BY period_start, zone_id, circuit_id, tariff_id, client_type;

    -- Disponibilidad AMI por dimensiones
    SELECT DATEFROMPARTS(l.period_year, l.period_month, 1) AS period_start, c.zone_id, c.circuit_id, c.tariff_id, c.client_type,
           COUNT(*) AS lecturas_esperadas,
           SUM(CASE WHEN l.ami_valid = 1 AND l.reading_status = N'Leído' THEN 1 ELSE 0 END) AS lecturas_validas
      INTO energrid.mat_ami_dim_mes_new
      FROM energrid.fact_lectura_energia l
      JOIN energrid.dim_medidor m ON m.meter_id = l.meter_id AND m.ami_enabled = 1
      JOIN energrid.dim_cliente c ON c.client_id = l.client_id
      CROSS JOIN energrid.cfg_parametros p
     WHERE DATEFROMPARTS(l.period_year, l.period_month, 1)
           BETWEEN DATEFROMPARTS(YEAR(p.periodo_desde), MONTH(p.periodo_desde), 1) AND p.periodo_hasta AND p.id = 1
     GROUP BY DATEFROMPARTS(l.period_year, l.period_month, 1), c.zone_id, c.circuit_id, c.tariff_id, c.client_type;

    CREATE CLUSTERED INDEX cx_dim ON energrid.mat_perdida_circuito_mes_new (period_start, zone_id, circuit_id);
    CREATE CLUSTERED INDEX cx_dim ON energrid.mat_cartera_dim_new          (snapshot_date, zone_id, circuit_id, tariff_id, client_type);
    CREATE CLUSTERED INDEX cx_dim ON energrid.mat_facturado_dim_new        (snapshot_date, zone_id, circuit_id, tariff_id, client_type);
    CREATE CLUSTERED INDEX cx_dim ON energrid.mat_cobranza_dim_mes_new     (period_start, zone_id, circuit_id, tariff_id, client_type);
    CREATE CLUSTERED INDEX cx_dim ON energrid.mat_ami_dim_mes_new          (period_start, zone_id, circuit_id, tariff_id, client_type);

    -- Intercambio atomico
    BEGIN TRAN;
        DROP TABLE IF EXISTS energrid.mat_perdida_circuito_mes; EXEC sp_rename N'energrid.mat_perdida_circuito_mes_new', N'mat_perdida_circuito_mes';
        DROP TABLE IF EXISTS energrid.mat_cartera_dim;          EXEC sp_rename N'energrid.mat_cartera_dim_new',          N'mat_cartera_dim';
        DROP TABLE IF EXISTS energrid.mat_facturado_dim;        EXEC sp_rename N'energrid.mat_facturado_dim_new',        N'mat_facturado_dim';
        DROP TABLE IF EXISTS energrid.mat_cobranza_dim_mes;     EXEC sp_rename N'energrid.mat_cobranza_dim_mes_new',     N'mat_cobranza_dim_mes';
        DROP TABLE IF EXISTS energrid.mat_ami_dim_mes;          EXEC sp_rename N'energrid.mat_ami_dim_mes_new',          N'mat_ami_dim_mes';

        IF OBJECT_ID(N'energrid.mat_resumen_estado', N'U') IS NOT NULL
        BEGIN
            DELETE FROM energrid.mat_resumen_estado;
            INSERT energrid.mat_resumen_estado (refrescado_en, duracion_seg) VALUES (SYSDATETIME(), DATEDIFF(SECOND, @t0, SYSDATETIME()));
        END
    COMMIT;
END;
GO

-- 4. Envoltorio transaccional con auditoria (usado por la API y Task Scheduler)
CREATE OR ALTER PROCEDURE energrid.usp_refrescar_resumenes_log
    @origen NVARCHAR(40) = N'manual'
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @@TRANCOUNT <> 0
        THROW 50001, N'Ejecute el refresco sin una transaccion externa.', 1;
    DECLARE @t0 DATETIME2(0) = SYSDATETIME(), @run BIGINT, @rc INT;
    DECLARE @res NVARCHAR(100) = N'energrid_usp_refrescar_resumenes';

    -- Exclusion mutua
    EXEC @rc = sp_getapplock @Resource = @res, @LockMode = N'Exclusive', @LockOwner = N'Session', @LockTimeout = 0;
    IF @rc < 0
    BEGIN
        INSERT energrid.aud_refresco_log (iniciado_en, finalizado_en, estado, duracion_seg, origen, host, error_mensaje)
        VALUES (@t0, SYSDATETIME(), 'OMITIDO', 0, @origen, HOST_NAME(), N'Ya hay otro refresco en curso.');
        RAISERROR(N'Refresco omitido: ya hay otro en curso.', 16, 1);
        RETURN 2;
    END;

    BEGIN TRY
        DELETE FROM energrid.aud_refresco_log WHERE iniciado_en < DATEADD(DAY, -400, SYSDATETIME());
        INSERT energrid.aud_refresco_log (iniciado_en, estado, origen, host) VALUES (@t0, 'EN_CURSO', @origen, HOST_NAME());
        SET @run = SCOPE_IDENTITY();

        BEGIN TRAN;
        EXEC energrid.usp_refrescar_resumenes;
        EXEC energrid.usp_refrescar_resumenes_dim;
        IF OBJECT_ID(N'energrid.usp_refrescar_rendimiento', N'P') IS NOT NULL
            EXEC energrid.usp_refrescar_rendimiento;

        DECLARE @f TABLE (fuente VARCHAR(30) PRIMARY KEY, fecha_dato_max DATE NULL, filas BIGINT NULL);
        INSERT @f SELECT 'facturacion',    MAX(invoice_date),                COUNT_BIG(*) FROM energrid.fact_facturacion;
        INSERT @f SELECT 'lecturas_ami',   MAX(reading_date),                COUNT_BIG(*) FROM energrid.fact_lectura_energia;
        INSERT @f SELECT 'pagos',          MAX(payment_date),                COUNT_BIG(*) FROM energrid.fact_pago;
        INSERT @f SELECT 'cartera',        MAX(snapshot_date),               COUNT_BIG(*) FROM energrid.fact_cartera_snapshot;
        INSERT @f SELECT 'interrupciones', CAST(MAX(start_datetime) AS DATE), COUNT_BIG(*) FROM energrid.fact_interrupcion;
        INSERT @f SELECT 'energia_diaria', MAX([date]),                      COUNT_BIG(*) FROM energrid.fact_energia_transformador_diaria;

        DELETE FROM energrid.mat_fuente_estado;
        INSERT energrid.mat_fuente_estado (fuente, fecha_dato_max, filas, medido_en)
        SELECT fuente, fecha_dato_max, filas, SYSDATETIME() FROM @f;

        DELETE FROM energrid.mat_resumen_estado;
        INSERT energrid.mat_resumen_estado (refrescado_en, duracion_seg)
        VALUES (SYSDATETIME(), DATEDIFF(SECOND, @t0, SYSDATETIME()));

        UPDATE energrid.aud_refresco_log
           SET estado = 'OK', finalizado_en = SYSDATETIME(), duracion_seg = DATEDIFF(SECOND, @t0, SYSDATETIME())
         WHERE run_id = @run;
        COMMIT;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK;
        UPDATE energrid.aud_refresco_log
           SET estado = 'ERROR', finalizado_en = SYSDATETIME(), duracion_seg = DATEDIFF(SECOND, @t0, SYSDATETIME()),
               error_numero = ERROR_NUMBER(), error_mensaje = LEFT(ERROR_MESSAGE(), 2000)
         WHERE run_id = @run;
        EXEC sp_releaseapplock @Resource = @res, @LockOwner = N'Session';
        THROW;
    END CATCH;

    EXEC sp_releaseapplock @Resource = @res, @LockOwner = N'Session';
END;
GO
