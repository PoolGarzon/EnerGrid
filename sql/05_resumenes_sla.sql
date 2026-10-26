-- 05_resumenes_sla.sql: Tablas de estado, configuracion de SLA y log de ejecuciones
USE EnerGrid;
GO

-- 1. Marca de ultimo refresco
IF OBJECT_ID(N'energrid.mat_resumen_estado', N'U') IS NULL
    CREATE TABLE energrid.mat_resumen_estado (refrescado_en DATETIME2(0) NOT NULL, duracion_seg INT NOT NULL);
GO

-- 2. Configuracion de SLA por fuente
IF OBJECT_ID(N'energrid.cfg_sla_fuente', N'U') IS NULL
    CREATE TABLE energrid.cfg_sla_fuente (
        fuente                   VARCHAR(30)   NOT NULL CONSTRAINT pk_cfg_sla_fuente PRIMARY KEY,
        orden                    TINYINT       NOT NULL,
        descripcion              NVARCHAR(120) NOT NULL,
        tabla_origen             NVARCHAR(128) NOT NULL,
        columna_fecha            NVARCHAR(128) NOT NULL,
        frecuencia               NVARCHAR(80)  NOT NULL,
        hora_limite              TIME(0)       NULL,
        max_horas_actualizacion  INT           NULL,
        responsable              NVARCHAR(120) NULL,
        estado_aprobacion        VARCHAR(20)   NOT NULL CONSTRAINT df_cfg_sla_aprob DEFAULT 'PROPUESTA_PENDIENTE'
                                 CONSTRAINT ck_cfg_sla_aprob CHECK (estado_aprobacion IN ('PROPUESTA_PENDIENTE', 'APROBADO')),
        aprobado_por             NVARCHAR(120) NULL,
        aprobado_en              DATETIME2(0)  NULL,
        nota                     NVARCHAR(300) NULL
    );
GO

-- Siembra inicial de fuentes SLA
;WITH s (fuente, orden, descripcion, tabla_origen, columna_fecha, frecuencia, hora_limite, max_h, nota) AS (
    SELECT * FROM (VALUES
     ('facturacion',   1, N'Facturacion',                           N'fact_facturacion',                 N'invoice_date',  N'Mensual (tras el cierre de facturacion)',    CAST('09:00' AS TIME(0)), 48, N'Propuesta tecnica: una factura por cliente y mes en el modelo.'),
     ('lecturas_ami',  2, N'Lecturas AMI',                          N'fact_lectura_energia',             N'reading_date',  N'Mensual (una lectura esperada por medidor y mes)', CAST('09:00' AS TIME(0)), 48, N'Propuesta tecnica: el modelo guarda una lectura por medidor y mes; si el origen real es diario, ajustar.'),
     ('pagos',         3, N'Pagos',                                 N'fact_pago',                        N'payment_date',  N'Diaria',                                      CAST('09:00' AS TIME(0)), 24, N'Propuesta tecnica.'),
     ('cartera',       4, N'Cartera (corte de saldos)',             N'fact_cartera_snapshot',            N'snapshot_date', N'Mensual (corte de fin de mes)',               CAST('09:00' AS TIME(0)), 24, N'Propuesta tecnica: un corte por mes en los datos actuales.'),
     ('interrupciones',5, N'Interrupciones del servicio',           N'fact_interrupcion',                N'start_datetime',N'Micro-lotes cada 30 min (propuesta no implementada)', CAST(NULL AS TIME(0)), 1, N'Propuesta academica; ingestion incremental y agregado rapido aun no implementados. Latencia maxima objetivo: 60 min.'),
     ('energia_diaria',6, N'Energia diaria de transformadores',     N'fact_energia_transformador_diaria',N'date',          N'Diaria',                                      CAST('09:00' AS TIME(0)), 24, N'Propuesta tecnica.'),
     ('resumenes',     7, N'Refresco de resumenes del dashboard (mat_*)', N'mat_resumen_estado',        N'refrescado_en', N'Diaria 08:10 (tarea programada; propuesto)',  CAST('09:00' AS TIME(0)), 26, N'Propuesta tecnica (no aprobada): tarea diaria 08:10, corte ingesta 06:00; refresco completo 08:10; disponibilidad 09:00; medir duracion de los 15 resumenes; 26 h = 24 h + 2 h de tolerancia, umbral de «Actualizacion atrasada».')
    ) v (fuente, orden, descripcion, tabla_origen, columna_fecha, frecuencia, hora_limite, max_h, nota)
)
INSERT energrid.cfg_sla_fuente (fuente, orden, descripcion, tabla_origen, columna_fecha, frecuencia, hora_limite, max_horas_actualizacion, responsable, nota)
SELECT s.fuente, s.orden, s.descripcion, s.tabla_origen, s.columna_fecha, s.frecuencia, s.hora_limite, s.max_h, NULL, s.nota
  FROM s
 WHERE NOT EXISTS (SELECT 1 FROM energrid.cfg_sla_fuente c WHERE c.fuente = s.fuente);
GO

-- 3. Registro de auditoria de ejecuciones
IF OBJECT_ID(N'energrid.aud_refresco_log', N'U') IS NULL
    CREATE TABLE energrid.aud_refresco_log (
        run_id         BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_aud_refresco_log PRIMARY KEY,
        iniciado_en    DATETIME2(0)  NOT NULL,
        finalizado_en  DATETIME2(0)  NULL,
        estado         VARCHAR(10)   NOT NULL CONSTRAINT ck_aud_refresco_estado CHECK (estado IN ('EN_CURSO', 'OK', 'ERROR', 'OMITIDO')),
        duracion_seg   INT           NULL,
        origen         NVARCHAR(40)  NULL,
        host           NVARCHAR(128) NULL,
        error_numero   INT           NULL,
        error_mensaje  NVARCHAR(2000) NULL
    );
GO

-- 4. Dato mas reciente por fuente al ultimo refresco OK
IF OBJECT_ID(N'energrid.mat_fuente_estado', N'U') IS NULL
    CREATE TABLE energrid.mat_fuente_estado (
        fuente          VARCHAR(30)  NOT NULL CONSTRAINT pk_mat_fuente_estado PRIMARY KEY,
        fecha_dato_max  DATE         NULL,
        filas           BIGINT       NULL,
        medido_en       DATETIME2(0) NOT NULL
    );
GO

-- 5. Ajustes de la propuesta técnica de SLA
SET XACT_ABORT ON;
BEGIN TRY
BEGIN TRAN;
UPDATE energrid.cfg_sla_fuente
   SET hora_limite = CAST('09:00' AS TIME(0)),
       nota = N'Propuesta: lote disponible para ingesta al corte 06:00; refresco de resumenes 08:10; disponibilidad en dashboard 09:00. Datos diarios: dia cerrado anterior; facturacion/AMI: ultimo cierre mensual.'
 WHERE fuente IN ('facturacion', 'lecturas_ami', 'pagos', 'cartera', 'energia_diaria')
   AND estado_aprobacion = 'PROPUESTA_PENDIENTE';
UPDATE energrid.cfg_sla_fuente
   SET frecuencia = N'Diaria 08:10 (tarea programada; propuesto)',
       hora_limite = CAST('09:00' AS TIME(0)), max_horas_actualizacion = 26,
       nota = N'Propuesta: publica los 15 resumenes juntos; inicio 08:10, disponible 09:00. Re-medir duracion completa; 26 h es tolerancia entre refrescos, no hora limite ni latencia de interrupciones.'
 WHERE fuente = 'resumenes' AND estado_aprobacion = 'PROPUESTA_PENDIENTE';
UPDATE energrid.cfg_sla_fuente
   SET frecuencia = N'Micro-lotes cada 30 min (propuesta no implementada)',
       hora_limite = NULL, max_horas_actualizacion = 1,
       nota = N'Propuesta academica no implementada: lote cada 30 min y disponibilidad <=60 min desde cambio en origen; staging validado, cursor externo y agregado rapido. No usar MAX(start_datetime) como cursor de cambios.'
 WHERE fuente = 'interrupciones' AND estado_aprobacion = 'PROPUESTA_PENDIENTE';
COMMIT;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK;
    THROW;
END CATCH;
GO
