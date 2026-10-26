# EnerGrid BI Control Center

Plataforma analítica y centro de control para la gestión integral de **pérdidas de energía**, **cobranza**, **calidad del servicio eléctrico (SAIDI/SAIFI)** y **disponibilidad AMI**, construida sobre SQL Server, FastAPI, Apache ECharts y Leaflet.

Base de datos: `EnerGrid` · Esquema: `energrid` · Datos: Sintéticos calibrados para distribución eléctrica.

---

## 1. Procedimiento completo de regeneración y ejecución desde cero

A continuación se detalla la secuencia ordenada de pasos para reconstruir y desplegar el proyecto íntegramente desde cero.

| Paso | Operación | Archivos clave | Carácter | ¿Por qué se necesita? |
|---|---|---|---|---|
| **1** | Entorno y dependencias | `requirements.txt`, `.venv` | **Obligatorio** | Aísla el entorno de Python 3.12 y provee las librerías necesarias (`fastapi`, `uvicorn`, `pyodbc`). |
| **2** | Generación de datos sintéticos | `sql\energrid_generator.py` | **Opcional** | Solo necesario si se desean regenerar los CSV crudos en `data\raw`. El repositorio ya los incluye. |
| **3** | ETL y carga en SQL Server | `sql\energrid_etl.py` | **Obligatorio** | Crea la base `EnerGrid`, el esquema `energrid`, las 13 tablas base, limpia los CSV y realiza la carga masiva. |
| **4** | Scripts SQL posteriores al ETL | `01`, `03`, `04`, `05` de `sql\` | **Obligatorio** | Configuración de parámetros, 20 vistas analíticas, snapshot de cartera y tablas materializadas. |
| **5** | Optimizaciones de rendimiento | `08` e `07` de `sql\` | **Opcional** | Índices adicionales y resúmenes para acelerar el Top 10 y filtros de alta dimensionalidad. |
| **6** | Verificaciones de consistencia | `sql\06_verificaciones.sql`, tests | **Recomendado** | Comprueba que los 6 controles de cuadre den `ok=1` y ejecuta las 69 pruebas del backend/frontend. |
| **7** | Despliegue de API y Dashboard | `server.py`, `static\index.html` | **Obligatorio** | Inicia el servicio FastAPI en el puerto 8000 y sirve la interfaz web interactiva. |

> [!NOTE]
> **Principio de no duplicación**: El ETL (`energrid_etl.py`) realiza una carga completa (elimina y recrea las 13 tablas base con sus PK, FK e índices). Los scripts SQL posteriores solo crean lo que el ETL deliberadamente no gestiona (vistas de negocio, snapshots, parámetros y tablas de refresco).

---

### Paso 1: Requisitos previos e instalación de dependencias (**Obligatorio**)

**Qué hace:** Valida la infraestructura del sistema operativo y aprovisiona el entorno virtual de Python.  
**Por qué se necesita:** Garantiza que la API y los scripts cuenten con los controladores ODBC y paquetes Python requeridos sin colisionar con dependencias del sistema.

- **Requisitos de sistema:**
  - Windows 10/11 con PowerShell 5.1+.
  - Python 3.12+ (64-bit).
  - Instancia de SQL Server activa (por defecto `localhost\SQLEXPRESS`) con permisos de `CREATE DATABASE`, `CREATE TABLE` y `CREATE PROCEDURE`.
  - **ODBC Driver 17 for SQL Server** instalado en el sistema.
  - Utilidad `sqlcmd` accesible en el `PATH`.
  - Navegador web moderno con acceso a CDN (para librerías ECharts/Leaflet y mapas OpenStreetMap).

Ejecuta en PowerShell desde la raíz del proyecto (`C:\Users\USUARIO\Downloads\energrid_web`):

```powershell
# 1.1 Limpiar y recrear entorno virtual limpio
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 1.2 Instalar dependencias exactas
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

### Paso 2: Generación de datos sintéticos (**Opcional**)

**Qué hace:** Ejecuta el generador probabilístico para regenerar los 13 archivos CSV en `data\raw\` y las respuestas de validación en `data\answer_key\`.  
**Por qué se necesita:** Solo se requiere si se desea simular un conjunto de datos completamente nuevo o con otros parámetros de distribución. Si los archivos CSV ya existen en `data\raw\`, este paso se omite.

```powershell
python sql\energrid_generator.py
```

---

### Paso 3: Ejecución del ETL y carga en SQL Server (**Obligatorio**)

**Qué hace:**
1. Crea la base de datos `EnerGrid` y el esquema `energrid` si no existen.
2. Crea las **13 tablas base** (8 dimensiones: `dim_zona`, `dim_subestacion`, `dim_circuito`, `dim_transformador`, `dim_tarifa`, `dim_cliente`, `dim_medidor`, `dim_causa`; y 5 hechos: `fact_lectura_energia`, `fact_facturacion`, `fact_pago`, `fact_interrupcion`, `fact_energia_transformador_diaria`) junto con sus llaves primarias, llaves foráneas e índices base.
3. Crea las 2 tablas de control de auditoría: `etl_lote` y `etl_rechazo`.
4. Limpia, valida e imputa reglas de negocio sobre los datos crudos, volcando registros descartados en `data\rejected\`.
5. Inserta masivamente los registros limpios en SQL Server.

**Por qué se necesita:** Es el cimiento de datos del almacén analítico. Cada ejecución es una carga completa e idempotente.

```powershell
# 3.1 (Opcional) Validación previa en seco sin tocar la base de datos:
python sql\energrid_etl.py --no-load

# 3.2 Carga completa en la base de datos:
python sql\energrid_etl.py --server "localhost\SQLEXPRESS" --database EnerGrid
```

---

### Paso 4: Ejecución de scripts SQL posteriores al ETL (**Obligatorio**)

**Qué hace:** Implementa las capas analítica, de agregación y de snapshots sobre el modelo cargado:
- `01_modelo.sql`: Crea la tabla de configuración `cfg_parametros` (metas, umbrales, pesos de criticidad) y la tabla de snapshot mensual `fact_cartera_snapshot`.
- `03_vistas_kpis.sql`: Crea las 20 vistas de negocio `vw_*` para reconciliación de energía, cobranza, calidad, criticidad, alertas y controles.
- `04_procedimientos.sql`: Crea los procedimientos almacenados de cálculo (`usp_refresh_cartera_snapshot` y `usp_refrescar_resumenes_log`).
- `05_resumenes_sla.sql`: Crea las tablas de registro y estado de refresco (`mat_resumen_estado`, `mat_resumen_log`) y la propuesta de SLA por fuente (`cfg_sla_fuente`).

**Por qué se necesita:** El ETL únicamente crea y puebla las tablas de hechos y dimensiones. Las vistas y procedimientos son requeridos para alimentar la API de FastAPI.

```powershell
$srv = 'localhost\SQLEXPRESS'

# 4.1 Aplicar scripts de base analítica en orden
foreach ($f in '01_modelo','03_vistas_kpis','04_procedimientos','05_resumenes_sla') {
    Write-Host "Aplicando $f.sql..." -ForegroundColor Cyan
    sqlcmd -S $srv -d EnerGrid -E -b -f 65001 -i "sql\$f.sql"
    if ($LASTEXITCODE -ne 0) { throw "Error al aplicar $f.sql" }
}

# 4.2 Ejecutar snapshot de cartera y primer refresco de tablas intermedias
Write-Host "Generando snapshot de cartera..." -ForegroundColor Yellow
sqlcmd -S $srv -d EnerGrid -E -b -l 15 -Q "EXEC energrid.usp_refresh_cartera_snapshot;"

Write-Host "Ejecutando refresco de resúmenes materializados..." -ForegroundColor Yellow
sqlcmd -S $srv -d EnerGrid -E -b -l 15 -t 3600 -Q "EXEC energrid.usp_refrescar_resumenes_log @origen=N'manual';"
```

---

### Paso 5: Aplicación de optimizaciones de rendimiento (**Opcional**)

**Qué hace:**
- `08_indices_rendimiento.sql`: Crea índices no agrupados dedicados (`ix_rend_*`) en las tablas de hechos para acelerar filtros por fecha, zona, circuito y tarifa.
- `07_resumenes_rendimiento.sql`: Construye resúmenes preagregados adicionales (como `mat_criticidad_transformador_full`) para que el Top 10 filtrado de transformadores responda en milisegundos en lugar de calcular en background.

**Por qué se necesita:** Muy recomendado para garantizar tiempos de respuesta menores a 200 ms al aplicar combinaciones de filtros en el dashboard.

```powershell
foreach ($f in '08_indices_rendimiento','07_resumenes_rendimiento') {
    Write-Host "Optimizando con $f.sql..." -ForegroundColor Cyan
    sqlcmd -S $srv -d EnerGrid -E -b -f 65001 -i "sql\$f.sql"
    if ($LASTEXITCODE -ne 0) { throw "Error al aplicar $f.sql" }
}

# Reejecutar el refresco para poblar los resúmenes de rendimiento
sqlcmd -S $srv -d EnerGrid -E -b -l 15 -t 3600 -Q "EXEC energrid.usp_refrescar_resumenes_log @origen=N'manual';"
```

---

### Paso 6: Ejecución de verificaciones (**Recomendado**)

**Qué hace:** Ejecuta los 6 controles de cuadre matemático en SQL Server y la suite de pruebas automatizadas del servidor web.  
**Por qué se necesita:** Garantiza que no exista doble conteo en la agregación de energía o facturación, que los saldos de cartera cuadren al 100% y que los endpoints de la API cumplan con todas las restricciones de seguridad y validación.

```powershell
# 6.1 Auditoría matemática en base de datos (Verificar que todos los controles tengan ok = 1)
sqlcmd -S $srv -d EnerGrid -E -b -f 65001 -i sql\06_verificaciones.sql

# 6.2 Pruebas automatizadas de la API, frontend y consistencia SQL (69 pruebas)
python tests\test_sin_navegador.py
```

---

### Paso 7: Inicio de la API y del dashboard (**Obligatorio**)

**Qué hace:** Arranca el servidor ASGI FastAPI que sirve la API REST y el frontend web.  
**Por qué se necesita:** Permite el acceso del usuario final a los tableros analíticos.

```powershell
python server.py
```

Acceso al dashboard: Abre en el navegador `http://127.0.0.1:8000`.

---

## 2. Fórmulas analíticas y definiciones completas del caso

Las definiciones y metodologías analíticas implementadas en el backend y documentadas para el caso EnerGrid son las siguientes:

### 2.1 Reconciliación de pérdidas de energía
- **Pérdida de energía (%):**
  $$\text{Pérdida \%} = \frac{\text{Energía Inyectada} - \text{Energía Facturada}}{\text{Energía Inyectada}}$$
- **Identidad aditiva exacta (sin doble conteo):**
  $$\text{Energía Inyectada} = \text{Pérdida Técnica} + \text{Pérdida No Técnica} + \text{Brecha de Medición} + \text{Energía Facturada}$$
  - **Pérdida de red:** $\text{Inyectada} - \text{Entregada}$ (fugas físicas de red más posible fraude o derivación clandestina).
  - **Pérdida técnica:** $\min(\text{Inyectada} - \text{Entregada}, \text{Inyectada} \times \text{perdida\_tecnica\_ref})$, con referencia del 7% definida en `cfg_parametros`.
  - **Pérdida no técnica (fraude/hurto):** $(\text{Inyectada} - \text{Entregada}) - \text{Pérdida Técnica}$.
  - **Brecha de medición:** $\text{Entregada} - \text{Facturada}$ (submedición por telemetría fallida, medidores dañados o consumo estimado).
  - **Pérdida evitable (USD):** $(\text{Inyectada} - \text{Facturada} - \text{Pérdida Técnica}) \times \text{Precio Medio USD/kWh}$. Representa el beneficio económico recuperable de las pérdidas no técnicas y la brecha de medición.

### 2.2 Cobranza y gestión de cartera
- **Collection Efficiency (CE %):**
  $$\text{CE \%} = \frac{\sum \text{Cobros recibidos}}{\sum \text{Facturación exigible}}$$
  Calculado sobre los meses con exigible mayor a cero (facturas cuya fecha de vencimiento cae en el período analizado). Mantiene la misma regla estricta en `vw_kpi_resumen`, API y dashboard; los cobros de meses sin exigible (como enero, cuyas facturas vencen en febrero) no inflan artificialmente el ratio.
- **Cartera > 60 días (Morosidad):** Saldo vencido de facturas abiertas con `days_overdue > 60` evaluado a la fecha de corte del snapshot.
  $$\% \text{ Morosidad >60d} = \frac{\text{Saldo vencido > 60 días}}{\text{Saldo total de cartera}}$$
  *Nota técnica:* La cartera es una medida **semiaditiva** (foto de saldos al corte mensual); no debe sumarse entre meses distintos.
- **DSO (Days Sales Outstanding):**
  $$\text{DSO (días)} = \frac{\text{Saldo total de cartera al corte}}{\text{Facturación de la ventana} / \text{Días de la ventana (hasta 90 días)}}$$
  Indica el promedio de días que tarda la distribuidora en cobrar sus ventas facturadas.

### 2.3 Calidad de suministro eléctrico (Continuidad del servicio)
- **SAIDI (System Average Interruption Duration Index):**
  $$\text{SAIDI} = \frac{\sum (\text{Duración en minutos} \times \text{Clientes afectados})}{\text{Clientes servidos activos}}$$
  Expresado en minutos de interrupción por cliente en el período analizado.
- **SAIFI (System Average Interruption Frequency Index):**
  $$\text{SAIFI} = \frac{\sum \text{Clientes afectados}}{\text{Clientes servidos activos}}$$
  Expresado en número de interrupciones promedio por cliente.
- **MTTR (Mean Time to Repair):**
  $$\text{MTTR} = \frac{\sum \text{Minutos de interrupción}}{\text{Número total de eventos de corte}}$$
  Tiempo medio de restauración de fallas imprevistas y planificadas.

### 2.4 Medición inteligente (AMI) y facturación estimada
- **Disponibilidad AMI:**
  $$\text{Disponibilidad AMI} = \frac{\sum \text{Lecturas válidas de medidores AMI}}{\sum \text{Lecturas esperadas de medidores AMI}}$$
  - **Lectura esperada:** La lectura programada de cada medidor con telemetría activa en el mes (estados «Leído», «Estimado» o «Faltante»; exactamente una por medidor y mes).
  - **Lectura válida:** Estado «Leído» con indicador de telemetría íntegra `ami_valid = 1`.
  - Al existir una única lectura esperada por medidor AMI y mes, equivale a la definición del caso: `medidores con lectura válida / medidores AMI activos del mes` (incluyendo medidores reemplazados durante su ventana de vigencia). Alimenta el KPI, la serie mensual, el mapa y la alerta con umbral mínimo de 93% (`alerta_ami_min`).
- **Consumo estimado (% de facturas):**
  $$\text{Consumo Estimado \%} = \frac{\sum \text{Facturas con lectura estimada o faltante}}{\sum \text{Facturas emitidas}}$$
  Porcentaje ponderado de facturas emitidas bajo estimación (no de volumen de kWh). Su meta de referencia es $1 - \text{alerta\_ami\_min}$ (7% con el umbral AMI actual de 93%).

### 2.5 Modelo multicriterio de criticidad
Score compuesto normalizado en escala 0–100 para priorizar zonas y transformadores de distribución:
$$\text{Score} = w_{\text{impacto}} \cdot N(\text{Impacto USD}) + w_{\text{pérdida}} \cdot N(\% \text{ Pérdida}) + w_{\text{morosidad}} \cdot N(\% \text{ Cartera >60d}) + w_{\text{calidad}} \cdot N(\text{Calidad})$$
Donde:
- $N(x)$: Normalización min-max entre el universo de entidades comparadas.
- $N(\text{Calidad}) = w_{\text{saidi}} \cdot N(\text{SAIDI}) + (1 - w_{\text{saidi}}) \cdot N(\text{SAIFI})$.
- Pesos por defecto en `energrid.cfg_parametros`: $w_{\text{impacto}} = 0.35$, $w_{\text{pérdida}} = 0.25$, $w_{\text{morosidad}} = 0.20$, $w_{\text{calidad}} = 0.20$ y $w_{\text{saidi}} = 0.60$.

---

## 3. Justificación de las vistas SQL y su relación con los visuales del dashboard

El sistema cuenta con **20 vistas SQL** especializadas en `sql\03_vistas_kpis.sql`. Cada vista cumple una función analítica concreta orientada a los objetivos estratégicos de EnerGrid: reducción de pérdidas, aceleración de cobranza, aseguramiento de la calidad de servicio y confiabilidad de telemetría.

A continuación se detalla la correspondencia estricta entre las vistas SQL, las consultas de la API en `server.py` y los elementos visuales del frontend en `static\index.html`:

| Vista SQL | Grano analítico | Visuales, gráficos y mapas que alimenta | Objetivo analítico y valor para EnerGrid |
|---|---|---|---|
| **`vw_kpi_resumen`** | Sistema (1 fila) | KPI Cards del Executive (`#k_exec`) y tarjetas de cabecera en Losses (`#k_loss`), Collections (`#k_col`) y Reliability (`#k_rel`). | Sintetiza en una sola lectura ejecutiva los 10 indicadores vitales de la compañía, permitiendo a la alta dirección diagnosticar en segundos desvíos respecto a las metas corporativas. |
| **`vw_perdida_transformador_mes`** | Transformador × Mes | Gráfico de Reconciliación en Cascada (`#c_wf`), Composición de Pérdida (`#c_comp`), Pérdida Mensual (`#c_lm`) y modal de detalle (`/api/transformer/{tid}`). | Resuelve la identidad exacta de balance energético. Permite separar pérdidas técnicas de pérdidas no técnicas (sospecha de fraude) y brechas de medición, cuantificando el impacto económico en USD evitable para orientar inspecciones de campo. |
| **`vw_perdida_zona_mes`** | Zona × Mes | Tendencia mensual de pérdida (`#c_loss`), Ranking de pérdidas por zona (`#c_lz`), y coloración de zonas en el Mapa Interactivo (`#zoneMap`). | Facilita el benchmarking territorial entre las 15 zonas de concesión, detectando zonas crónicas por encima del umbral técnico del 9.4% y patrones de estacionalidad. |
| **`vw_facturacion_detalle`** | Factura individual | Base de cálculo de `vw_perdida_transformador_mes`, `vw_consumo_estimado_mes` y detalle de facturas en drill-through (`/api/client/{cid}`). | Proporciona trazabilidad completa desde el cliente hasta el transformador y circuito, evitando duplicaciones de facturación al agregar datos comerciales y técnicos. |
| **`vw_clientes_servidos`** | Zona / Circuito / Transformador | Denominador en los gráficos de SAIDI y SAIFI (`#c_saidi_mini`, `#c_saidi`, `#c_saidi_z`) y tablas de confiabilidad. | Garantiza un denominador consistente y oficial de clientes activos servidos, eliminando distorsiones en los cálculos regulatorios de continuidad de suministro. |
| **`vw_cliente_patron_consumo`** | Cliente | Gráfico de Cartera por Tipo de Cliente (`#c_seg`) y Tabla de Detalle por Segmento de Consumo (`#t_seg`). | Segmenta a los usuarios por cuartiles de consumo y variabilidad (estable vs. volátil). Permite priorizar la gestión de cobranza y fiscalización según el perfil de consumo. |
| **`vw_cartera_snapshot`** | Factura al corte mensual | Gráfico de Cartera por Antigüedad (`#c_aging`), Cartera por Tarifa (`#c_tarifa`, `#t_tarifa`) y clientes con saldo en el transformador (`/api/transformer/{tid}/clients`). | Permite auditar la estructura de envejecimiento de la deuda (aging buckets: 0-30, 31-60, 61-90, >90 días) sin distorsión por agregación temporal, focalizando el recupero en saldos con mora mayor a 60 días. |
| **`vw_cobranza_mes`** | Mes × Zona × Tarifa × Tipo | Tendencia de Collection Efficiency (`#c_ce_mini` en Executive y `#c_ce` en Collections) y comparación de cobros vs. facturación exigible. | Mide la efectividad real del ciclo de recaudo frente a la meta del 95%, aislando el dinero cobrado respecto a la deuda legalmente exigible de cada mes. |
| **`vw_dso_zona_mes`** | Snapshot × Zona | KPI de DSO en días, gráfico de Morosidad >60 días por Zona (`#c_m60`) y normalización de morosidad en el índice de criticidad. | Monitorea el ciclo de conversión de ventas a caja y detecta zonas territoriales con deterioro en hábitos de pago antes de que se vuelvan incobrables. |
| **`vw_ami_mes`** | Mes × Zona × Circuito × Transformador | Serie mensual de Disponibilidad AMI (`#c_ami`), indicador en tooltip del mapa (`#zoneMap`) y Alerta de Telemetría Baja (`#c_alert`, `#t_svc`). | Supervisa la salud de los medidores inteligentes con telemetría activa. Permite anticipar fallas de comunicación antes de que deriven en facturas estimadas. |
| **`vw_consumo_estimado_mes`** | Mes × Zona × Circuito × Tarifa × Tipo | Gráfico de Facturas con Consumo Estimado (`#c_est`) y KPI de facturación estimada (`#k_loss`). | Identifica distorsiones comerciales donde no se pudo obtener lectura real, alertando cuando el volumen de facturación estimada supera la tolerancia técnica del 7%. |
| **`vw_interrupciones_detalle`** | Evento de interrupción | Series temporales de fallas, Gráfico de Top Causas de Interrupción (`#c_cause`), y auditoría de eventos programados vs. accidentales. | Registra la magnitud de cada contingencia en cliente-minutos afectados y georreferenciación de fallas, fundamental para el análisis de causa raíz en la red de media y baja tensión. |
| **`vw_saidi_saifi_zona_mes`** | Mes × Zona | Gráfico mensual combinado SAIDI/SAIFI (`#c_saidi`), SAIDI acumulado por zona (`#c_saidi_z`) y Alerta de Deterioro de SAIDI. | Diagnostica la calidad de suministro a nivel zonal, comparando la frecuencia de cortes frente a la duración media de reposición del servicio. |
| **`vw_saidi_saifi_circuito_causa`** | Circuito × Causa | Distribución de Causas de Falla (`#c_cause`) y diagnóstico de confiabilidad por alimentador. | Orienta el plan de mantenimiento preventivo (p. ej. desrame de árboles, cambio de transformadores o protecciones) atacando las causas que generan más cliente-minutos perdidos. |
| **`vw_circuitos_recurrentes`** | Circuito | Tabla interactiva de Circuitos Recurrentes Críticos (`#t_rec`). | Aísla los circuitos con fallas crónicas (eventos en el percentil 90 o superior y afectados en 6 o más meses del año), guiando inversiones estructurales de reposición de activos. |
| **`vw_criticidad_zona`** | Zona | Gráfico Top 10 Zonas por Criticidad (`#c_crit`) y semáforo cromático de zonas en el Mapa interactivo (`#zoneMap`). | Consolida pérdidas, morosidad y calidad de servicio en una sola métrica multivariable, permitiendo asignar presupuestos operativos a las zonas con mayor vulnerabilidad integral. |
| **`vw_criticidad_transformador`** | Transformador | Base del ranking de transformadores críticos y de las consultas de optimización en segundo plano. | Aplica el modelo multicriterio al activo más cercano al usuario final, permitiendo priorizar el mantenimiento de transformadores con alto impacto en pérdidas y cobranza. |
| **`vw_top10_transformadores`** | Transformador (Top 10) | Tabla de Intervención Prioritaria de Transformadores (`#t_top10`) con modal drill-through. | Entrega una lista de trabajo inmediata con el beneficio económico en USD recuperables, el SAIDI del sistema evitable y el componente dominante causante de la criticidad. |
| **`vw_alertas`** | Entidad × Mes | Gráfico de Alertas por Tipo y Severidad (`#c_alert`), badges de estado y Tabla de Alertas de Calidad y Medición (`#t_svc`). | Implementa supervisión por excepción mediante 3 reglas analíticas automatizadas: pérdida anómala por z-score (>2σ), deterioro de SAIDI (>25% vs. media móvil previa) y disponibilidad AMI crítica (<93%). |
| **`vw_control_reconciliacion`** | Control (6 pruebas) | Tabla interactiva Matriz de Control y Reconciliación (`#t_ctrl`). | Proporciona evidencia matemática inmutable de que los datos no contienen doble conteo, que las sumas de energía coinciden con los hechos diarios y que el balance de cartera es exacto. |

---

## 4. Arquitectura de resúmenes precalculados, refresco y automatización

Para evitar que el dashboard recalcule millones de registros en cada clic, EnerGrid implementa una **arquitectura de materialización desacoplada** basada en tablas intermedias (`mat_*`).

### 4.1 ¿Por qué el dashboard responde en milisegundos?
- **Sin resúmenes (cálculo al vuelo sobre vistas):** Las consultas deben agregar directamente sobre las tablas de hechos (`fact_lectura_energia`, `fact_facturacion`, `fact_pago`, `fact_energia_transformador_diaria` y `fact_cartera_snapshot`), escaneando más de 4.3 millones de filas, ejecutando funciones de ventana (`STDEV`, `NTILE`), ordenamientos y uniones pesadas. Esto generaría latencias de 3 a 15 segundos por filtro y sobrecargaría la CPU de SQL Server.
- **Con resúmenes precalculados (`mat_*`):** Las peticiones del dashboard consultan tablas compactas preagregadas (por ejemplo, `mat_perdida_zona_mes` tiene solo 180 filas y `mat_cartera_resumen` ~10 mil filas en lugar de 1.67 millones). La API resuelve los tableros en **5 a 40 ms**.

---

### 4.2 Lo que YA está implementado

| Componente | Objeto / Archivo | Estado actual | Función que desempeña |
|---|---|---|---|
| **Tablas materializadas de zona (10)** | `energrid.mat_*` | **Implementado** | Tablas de resumen preagregadas por zona y mes (`mat_perdida_zona_mes`, `mat_cobranza_mes`, `mat_ami_mes`, `mat_cartera_resumen`, etc.). |
| **Tablas materializadas dimensionales (5)** | `energrid.mat_*_dim*` | **Implementado** | Resúmenes con corte por circuito, tarifa y tipo de cliente (`mat_cartera_dim`, `mat_perdida_circuito_mes`, etc.). |
| **Tablas de rendimiento (6)** | `energrid.mat_*_full*` | **Implementado** | Materialización profunda de transformadores y circuitos (`mat_criticidad_transformador_full`, etc.). |
| **Control de auditoría y heartbeat** | `mat_resumen_estado`, `aud_refresco_log` | **Implementado** | Registra el timestamp único de actualización (`refrescado_en`), duración en segundos e historial de ejecuciones (`OK`, `ERROR`, `OMITIDO`). |
| **Procedimiento maestro de refresco** | `energrid.usp_refrescar_resumenes_log` | **Implementado** | Orquestador atómico. Usa `sp_getapplock` para evitar ejecuciones concurrentes, calcula las tablas en `*_new`, hace el intercambio con `sp_rename` y registra la auditoría. |
| **Invalidación automática de caché** | `server.py` (`mats()`, `_mat["stamp"]`) | **Implementado** | Sondea `mat_resumen_estado` cada 30 s. Si detecta un nuevo refresco, purga automáticamente la memoria RAM (`_cache`) y el disco (`.cache/top10.json`). |
| **API de refresco en background** | `server.py` (`POST /api/refresh`) | **Implementado** | Endpoint asíncrono no bloqueante que dispara `usp_refrescar_resumenes_log @origen=N'WebDashboard'` con control de concurrencia. |
| **Botón en interfaz web** | `#btnRefresh` en `static/index.html` | **Implementado** | Botón interactivo con spinner y sondeo activo que recarga automáticamente el dashboard al finalizar. |
| **Monitor de estado en la UI** | `estado_datos.py`, `#dataStatus` en `static/index.html` | **Implementado** | Muestra el estado en tiempo real (*"Datos actualizados al DD/MM/AAAA HH:mm · Al día"* o *"Actualizando…"*) consultando `/api/data-status`. |
| **Script de refresco manual** | `scripts\refrescar_resumenes.ps1` | **Implementado** | Script PowerShell para ejecutar el refresco desde terminal o automatización externa vía `sqlcmd`. |
| **Script del Programador de Tareas** | `scripts\registrar_tarea_refresco.ps1` | **Implementado** | Script para instalar, consultar o actualizar la tarea programada en Windows Task Scheduler. |

#### Proceso de refresco manual
Se puede ejecutar desde la propia interfaz web mediante el botón **«Actualizar resúmenes»**, directamente en PowerShell:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refrescar_resumenes.ps1
```
O directamente en SQL Server:
```sql
EXEC energrid.usp_refrescar_resumenes_log @origen = N'manual';
```

---

### 4.3 Funcionamiento del botón web, fecha de actualización y tarea programada

#### A. Botón exclusivo «Actualizar resúmenes» en el Dashboard Web
- **Reubicación y diseño:**
  - El botón se encuentra integrado en el bloque de **Análisis Temporal** (`.filters.temporal`), a la derecha de la selección de meses y el botón de restablecimiento.
  - Se distingue claramente con un **estilo esmeralda / menta profesional** (`background: #ecfdf5; border-color: #10b981; color: #065f46`), contrastando armónicamente con los controles azules y grises sin perder la estética clara de la plataforma.
- **Información visible de última actualización:**
  - Junto al botón se muestra de forma permanente y destacada:
    - **Punto de estado (`#refreshDot`):** Verde = *Al día*, Naranja = *Atrasado / Sin datos*, Rojo = *Error*, Verde pulsante = *Actualizando…*.
    - **Fecha y hora de actualización (`#refreshTimestamp`):** Indica exactamente hasta qué fecha y hora están reconstruidos los resúmenes (`DD/MM/AAAA HH:mm`), proveniente de `energrid.mat_resumen_estado.refrescado_en`.
    - **Etiqueta de origen (`#refreshOrigin`):** Especifica quién ejecutó la última actualización:
      - `Tarea Windows`: Ejecutada automáticamente por el Programador de tareas de Windows.
      - `Web Dashboard`: Ejecutada manualmente por un usuario desde el botón del dashboard.
      - `Manual SQL`: Ejecutada por un administrador vía script PowerShell o procedimiento T-SQL directo.
    - **Duración (`#refreshDuration`):** Tiempo en segundos que tomó la última materialización (ej. `(162 s)`).
- **Flujo operativo end-to-end:**
  1. El usuario pulsa **«Actualizar resúmenes»**.
  2. El botón se deshabilita de inmediato, activa una animación de giro en su ícono y cambia su texto a *"Actualizando…"*.
  3. Envía una petición `POST /api/refresh` a la API.
  4. En `server.py`, el endpoint verifica que no exista otra ejecución activa (vía cerrojo en memoria `_refresh_lock` y consulta a `estado_datos()`) y despacha la ejecución de `energrid.usp_refrescar_resumenes_log @origen=N'WebDashboard'` en un hilo secundario en segundo plano, respondiendo `HTTP 202 Aceptado` en milisegundos.
  5. En SQL Server, `usp_refrescar_resumenes_log` adquiere un cerrojo exclusivo a nivel de sesión con `sp_getapplock` para impedir concurrencias y reconstruye de manera atómica las 21 tablas `mat_*`.
  6. El frontend cambia a modo de sondeo frecuente (cada 3 segundos) consultando `/api/data-status`.
  7. Cuando SQL Server finaliza y actualiza `mat_resumen_estado` con el nuevo timestamp y `aud_refresco_log` con `estado = 'OK'`, el backend purga automáticamente su caché de memoria y disco.
  8. El frontend detecta la finalización exitosa, actualiza la fecha y hora en pantalla, restaura el botón a su estado normal y dispara automáticamente la función `load()`, recargando de inmediato todos los gráficos, tarjetas de KPI y tablas del dashboard con los nuevos datos precalculados.

#### B. Registro y configuración de la Tarea Programada de Windows (Task Scheduler)
Dado que SQL Server Express no cuenta con el servicio SQL Server Agent, la automatización periódica oficial se implementa mediante el **Programador de Tareas de Windows (Task Scheduler)**.

##### 1. Configurar la tarea programada
Abre una consola de **PowerShell con privilegios de Administrador** (clic derecho > *Ejecutar como administrador*) y ejecuta desde la raíz del proyecto:

```powershell
# Instalar la tarea programada diaria (ejemplo: ejecución a las 08:10 AM, hora por defecto):
.\scripts\registrar_tarea_refresco.ps1 -Accion Instalar -Hora 08:10 -Confirmar

# O para cambiar el horario de una tarea ya existente:
.\scripts\registrar_tarea_refresco.ps1 -Accion Actualizar -Hora 06:00 -Confirmar
```

> [!NOTE]
> La tarea registra sus ejecuciones con `origen = TaskScheduler` (argumento `-Origen TaskScheduler`). Una tarea creada antes de este cambio no lo incluye y etiquetaría sus ejecuciones como `manual`: vuelve a registrarla una vez con `-Accion Quitar -Confirmar` y luego `-Accion Instalar -Hora 08:10 -Confirmar`. Las ejecuciones manuales del script (`refrescar_resumenes.ps1`) se etiquetan como `Manual SQL`.

##### 2. Verificar el estado de la tarea
Para comprobar que la tarea quedó correctamente registrada y conocer cuándo fue su última ejecución y cuándo será la próxima:

```powershell
.\scripts\registrar_tarea_refresco.ps1 -Accion Estado
```
*Salida esperada:*
```text
Tarea:             EnerGrid - Refresco de resumenes
Estado actual:     Ready (habilitada)
Disparador:        08:10 (todos los dias)
Ultima ejecucion:  25/10/2026 23:07:34  (resultado: 0x0)
Proxima ejecucion: 26/10/2026 08:10:00
```

##### 3. Ejecutar la tarea bajo demanda
Puedes probar la tarea de Windows de inmediato sin esperar al horario programado:

```powershell
# Disparar la tarea de Windows directamente:
Start-ScheduledTask -TaskName "EnerGrid - Refresco de resumenes"

# O ejecutar el script de PowerShell que invoca la tarea:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refrescar_resumenes.ps1
```

##### 4. Verificar el funcionamiento y registro dual
Cuando la tarea programada finaliza:
1. **En el archivo de registro en disco:** Se actualiza `logs\refresco_AAAAMM.log` con el timestamp de inicio, salida de sqlcmd y `FIN OK`.
2. **En la base de datos SQL Server:** Se registra una fila en `energrid.aud_refresco_log` con `origen = 'TaskScheduler'` y `estado = 'OK'`:
   ```sql
   SELECT TOP 3 run_id, origen, iniciado_en, finalizado_en, duracion_seg, estado
   FROM energrid.aud_refresco_log
   ORDER BY run_id DESC;
   ```
3. **En la interfaz del Dashboard:** Al abrir o refrescar el dashboard, el panel de resúmenes reflejará automáticamente:
   - La fecha y hora exacta de la ejecución realizada por Windows (`Resúmenes al: DD/MM/AAAA HH:mm`).
   - La etiqueta **`Tarea Windows`**.
   - El punto de estado en verde (`Al día`).
   - La duración en segundos de la ejecución.

---

## 5. Estructura de directorios

> Los archivos retirados del proyecto se movieron a `_para_eliminar\` (no se borraron). Para recuperar uno, muévelo de vuelta a su ruta original. Las carpetas `.cache\`, `__pycache__\` y `data\rejected\` se regeneran solas al ejecutar la aplicación o el ETL.

```
energrid_web/
├── config.py                 # Conexión parametrizada a SQL Server y configuración del esquema
├── server.py                 # API REST (FastAPI) con consultas concurrentes y caché automática
├── estado_datos.py           # Endpoint de auditoría del estado del refresco y SLA
├── requirements.txt          # Dependencias de Python (FastAPI, Uvicorn, pyodbc)
├── data/
│   ├── raw/                  # Archivos CSV crudos de entrada
│   ├── answer_key/           # Benchmarks de validación de requisitos
│   └── rejected/             # Registros rechazados por el ETL (se genera al ejecutar energrid_etl.py)
├── sql/
│   ├── energrid_generator.py # Generador sintético de datos probabilísticos
│   ├── energrid_etl.py       # Script principal de ETL, limpieza y carga
│   ├── energrid_sqlserver.py # Creación de tablas base e inserción masiva
│   ├── 01_modelo.sql         # Parámetros de negocio y tabla de snapshot
│   ├── 03_vistas_kpis.sql    # 20 vistas analíticas de KPIs y reconciliación
│   ├── 04_procedimientos.sql # Procedimientos de snapshot y materialización
│   ├── 05_resumenes_sla.sql  # Registro de auditoría del refresco y catálogo SLA
│   ├── 06_verificaciones.sql# 6 controles matemáticos de cuadre y consistencia
│   ├── 07_resumenes_rendimiento.sql # Resúmenes adicionales para acelerar el Top 10
│   └── 08_indices_rendimiento.sql   # Índices no agrupados de alto desempeño
├── static/
│   └── index.html            # Interfaz SPA interactiva (ECharts + Leaflet)
├── scripts/
│   ├── refrescar_resumenes.ps1       # Script de ejecución manual del refresco
│   └── registrar_tarea_refresco.ps1  # Gestor de la tarea programada de Windows
└── tests/
    ├── test_sin_navegador.py        # Suite de 69 pruebas automatizadas
    ├── verificar_combinaciones.py   # Auditoría de combinaciones de filtros de la API
    └── benchmark_endpoints.py       # Medición de latencia y concurrencia HTTP
```
