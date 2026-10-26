-- 08_indices_rendimiento.sql: Creacion de indices no agrupados en tablas materializadas
USE EnerGrid;
GO

CREATE OR ALTER PROCEDURE energrid.usp_indices_rendimiento AS
BEGIN
    SET NOCOUNT ON;
    IF OBJECT_ID(N'energrid.mat_criticidad_transformador_full', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_criticidad_transformador_full') AND name=N'ix_rend_zona_score')
        CREATE NONCLUSTERED INDEX [ix_rend_zona_score] ON energrid.[mat_criticidad_transformador_full] (zone_id, score_criticidad DESC, transformer_id) INCLUDE (circuit_id, ranking, transformer_name, impacto_usd, perdida_pct, morosidad_pct, norm_impacto, norm_perdida, norm_morosidad, norm_calidad);
    IF OBJECT_ID(N'energrid.mat_criticidad_transformador_full', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_criticidad_transformador_full') AND name=N'ix_rend_circuito_score')
        CREATE NONCLUSTERED INDEX [ix_rend_circuito_score] ON energrid.[mat_criticidad_transformador_full] (circuit_id, score_criticidad DESC, transformer_id) INCLUDE (zone_id, ranking, transformer_name, impacto_usd, perdida_pct, morosidad_pct, norm_impacto, norm_perdida, norm_morosidad, norm_calidad);
    IF OBJECT_ID(N'energrid.mat_perdida_transformador_mes', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_perdida_transformador_mes') AND name=N'ix_rend_energia_mes')
        CREATE NONCLUSTERED INDEX [ix_rend_energia_mes] ON energrid.[mat_perdida_transformador_mes] (period_year, period_month, transformer_id) INCLUDE (zone_id, circuit_id, injected_kwh, perdida_total_kwh, perdida_evitable_usd);
    IF OBJECT_ID(N'energrid.mat_interrupciones_detalle', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_interrupciones_detalle') AND name=N'ix_rend_servicio_circuito_mes')
        CREATE NONCLUSTERED INDEX [ix_rend_servicio_circuito_mes] ON energrid.[mat_interrupciones_detalle] (circuit_id, period_start) INCLUDE (zone_id, transformer_id, cause_name, customers_affected, cliente_minutos, duration_minutes);
    IF OBJECT_ID(N'energrid.mat_saidi_saifi_zona_mes', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_saidi_saifi_zona_mes') AND name=N'ix_rend_servicio_zona_mes')
        CREATE NONCLUSTERED INDEX [ix_rend_servicio_zona_mes] ON energrid.[mat_saidi_saifi_zona_mes] (zone_id, period_start) INCLUDE (clientes_servidos, cliente_minutos, clientes_interrumpidos, eventos, minutos);
    IF OBJECT_ID(N'energrid.mat_cartera_dim', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_cartera_dim') AND name=N'ix_rend_cartera_zona_corte')
        CREATE NONCLUSTERED INDEX [ix_rend_cartera_zona_corte] ON energrid.[mat_cartera_dim] (zone_id, snapshot_date, circuit_id, tariff_id, client_type) INCLUDE (saldo, saldo_mayor_60, facturas, aging_bucket, aging_order, segmento_consumo);
    IF OBJECT_ID(N'energrid.mat_facturado_dim', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_facturado_dim') AND name=N'ix_rend_facturado_zona_corte')
        CREATE NONCLUSTERED INDEX [ix_rend_facturado_zona_corte] ON energrid.[mat_facturado_dim] (zone_id, snapshot_date, circuit_id, tariff_id, client_type) INCLUDE (facturado_ventana, dias_ventana);
    IF OBJECT_ID(N'energrid.mat_consumo_estimado_mes', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.mat_consumo_estimado_mes') AND name=N'ix_rend_filtros')
        CREATE NONCLUSTERED INDEX [ix_rend_filtros] ON energrid.[mat_consumo_estimado_mes] (zone_id, circuit_id, tariff_id, client_type, period_start) INCLUDE (facturas_emitidas, facturas_estimadas);
    IF OBJECT_ID(N'energrid.fact_cartera_snapshot', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.fact_cartera_snapshot') AND name=N'ix_rend_snapshot_cliente')
        CREATE NONCLUSTERED INDEX [ix_rend_snapshot_cliente] ON energrid.[fact_cartera_snapshot] (snapshot_date, client_id) INCLUDE (balance, days_overdue);
    IF OBJECT_ID(N'energrid.fact_facturacion', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.fact_facturacion') AND name=N'ix_rend_facturacion_periodo')
        CREATE NONCLUSTERED INDEX [ix_rend_facturacion_periodo] ON energrid.[fact_facturacion] (period_year, period_month) INCLUDE (client_id, billed_kwh);
    IF OBJECT_ID(N'energrid.fact_interrupcion', N'U') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'energrid.fact_interrupcion') AND name=N'ix_rend_interrupcion_inicio')
        CREATE NONCLUSTERED INDEX [ix_rend_interrupcion_inicio] ON energrid.[fact_interrupcion] (start_datetime, circuit_id) INCLUDE (zone_id, cause_id, customers_affected, duration_minutes);
END;
GO
EXEC energrid.usp_indices_rendimiento;
GO
