-- 01_modelo.sql: Parametros de configuracion y snapshot de cartera
IF DB_ID(N'EnerGrid') IS NULL CREATE DATABASE [EnerGrid];
GO
USE EnerGrid;
GO
IF SCHEMA_ID(N'energrid') IS NULL EXEC(N'CREATE SCHEMA [energrid]');
GO

-- 1. Parametros editables de configuracion
IF OBJECT_ID(N'energrid.cfg_parametros', N'U') IS NULL
    CREATE TABLE [energrid].[cfg_parametros] (
        [id]                      INT          NOT NULL CONSTRAINT [PK_cfg_parametros] PRIMARY KEY CONSTRAINT [CK_cfg_id] CHECK ([id] = 1),
        [fecha_corte]             DATE         NOT NULL,
        [periodo_desde]           DATE         NOT NULL,
        [periodo_hasta]           DATE         NOT NULL,
        [perdida_tecnica_ref]     DECIMAL(5,4) NOT NULL,
        [incluir_planificadas]    BIT          NOT NULL,
        [w_impacto]               DECIMAL(4,3) NOT NULL,
        [w_perdida]               DECIMAL(4,3) NOT NULL,
        [w_morosidad]             DECIMAL(4,3) NOT NULL,
        [w_calidad]               DECIMAL(4,3) NOT NULL,
        [w_saidi_en_calidad]      DECIMAL(4,3) NOT NULL,
        [alerta_zscore_perdida]   DECIMAL(4,2) NOT NULL,
        [alerta_saidi_deterioro]  DECIMAL(5,2) NOT NULL,
        [alerta_ami_min]          DECIMAL(5,4) NOT NULL,
        CONSTRAINT [CK_cfg_pesos] CHECK (ABS([w_impacto] + [w_perdida] + [w_morosidad] + [w_calidad] - 1) < 0.0005)
    );
GO

-- Siembra inicial de parametros
IF NOT EXISTS (SELECT 1 FROM [energrid].[cfg_parametros])
    INSERT INTO [energrid].[cfg_parametros] VALUES
        (1, '2025-12-31', '2025-01-01', '2025-12-31', 0.07, 1, 0.35, 0.25, 0.20, 0.20, 0.50, 2.0, 0.25, 0.93);
GO

-- 2. Snapshot mensual de cartera
IF OBJECT_ID(N'energrid.fact_cartera_snapshot', N'U') IS NULL
    CREATE TABLE [energrid].[fact_cartera_snapshot] (
        [snapshot_date] DATE          NOT NULL,
        [invoice_id]    INT           NOT NULL,
        [client_id]     INT           NOT NULL,
        [amount_due]    DECIMAL(14,2) NOT NULL,
        [paid_amount]   DECIMAL(14,2) NOT NULL,
        [balance]       DECIMAL(14,2) NOT NULL,
        [days_overdue]  INT           NOT NULL,
        [aging_bucket]  NVARCHAR(10)  NOT NULL,
        [aging_order]   TINYINT       NOT NULL,
        CONSTRAINT [PK_fact_cartera_snapshot] PRIMARY KEY CLUSTERED ([snapshot_date], [invoice_id])
    );
GO
