-- 07_resumenes_rendimiento.sql: Materializaciones adicionales para acelerar el Top 10
USE EnerGrid;
GO

CREATE OR ALTER PROCEDURE energrid.usp_refrescar_rendimiento AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    IF @@TRANCOUNT = 0
        THROW 50002, N'Ejecute energrid.usp_refrescar_resumenes_log, no este componente aislado.', 1;

    DROP TABLE IF EXISTS energrid.mat_criticidad_transformador_full_new;
    SELECT * INTO energrid.mat_criticidad_transformador_full_new FROM energrid.vw_criticidad_transformador;
    DROP TABLE IF EXISTS energrid.mat_perdida_transformador_mes_new;
    SELECT * INTO energrid.mat_perdida_transformador_mes_new FROM energrid.vw_perdida_transformador_mes;
    DROP TABLE IF EXISTS energrid.mat_interrupciones_detalle_new;
    SELECT * INTO energrid.mat_interrupciones_detalle_new FROM energrid.vw_interrupciones_detalle;
    DROP TABLE IF EXISTS energrid.mat_clientes_servidos_new;
    SELECT * INTO energrid.mat_clientes_servidos_new FROM energrid.vw_clientes_servidos;
    DROP TABLE IF EXISTS energrid.mat_saidi_saifi_zona_mes_new;
    SELECT * INTO energrid.mat_saidi_saifi_zona_mes_new FROM energrid.vw_saidi_saifi_zona_mes;
    DROP TABLE IF EXISTS energrid.mat_circuitos_recurrentes_new;
    SELECT * INTO energrid.mat_circuitos_recurrentes_new FROM energrid.vw_circuitos_recurrentes;

    DROP TABLE IF EXISTS energrid.mat_criticidad_transformador_full;
    EXEC sp_rename N'energrid.mat_criticidad_transformador_full_new', N'mat_criticidad_transformador_full';
    DROP TABLE IF EXISTS energrid.mat_perdida_transformador_mes;
    EXEC sp_rename N'energrid.mat_perdida_transformador_mes_new', N'mat_perdida_transformador_mes';
    DROP TABLE IF EXISTS energrid.mat_interrupciones_detalle;
    EXEC sp_rename N'energrid.mat_interrupciones_detalle_new', N'mat_interrupciones_detalle';
    DROP TABLE IF EXISTS energrid.mat_clientes_servidos;
    EXEC sp_rename N'energrid.mat_clientes_servidos_new', N'mat_clientes_servidos';
    DROP TABLE IF EXISTS energrid.mat_saidi_saifi_zona_mes;
    EXEC sp_rename N'energrid.mat_saidi_saifi_zona_mes_new', N'mat_saidi_saifi_zona_mes';
    DROP TABLE IF EXISTS energrid.mat_circuitos_recurrentes;
    EXEC sp_rename N'energrid.mat_circuitos_recurrentes_new', N'mat_circuitos_recurrentes';

    IF OBJECT_ID(N'energrid.usp_indices_rendimiento', N'P') IS NOT NULL
        EXEC energrid.usp_indices_rendimiento;
END;
GO
