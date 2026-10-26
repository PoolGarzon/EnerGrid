<#
.SYNOPSIS
  Refresca los resumenes mat_* de EnerGrid con sqlcmd y deja registro (archivo de log + energrid.aud_refresco_log).

.DESCRIPTION
  Ejecuta SOLO  EXEC energrid.usp_refrescar_resumenes_log  (sql\04_procedimientos.sql). NO ejecuta el ETL, NO recarga
  tablas de hechos y NO toca nada mas. Usa la sesion de Windows (-E), igual que config.py.
  Codigos de salida: 0 = OK | 3 = sqlcmd no encontrado | otro = codigo de sqlcmd (error SQL, conexion o tiempo agotado).
  Log de archivo: <proyecto>\logs\refresco_AAAAMM.log (un archivo por mes).

  Uso manual (no requiere el Programador de tareas):
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refrescar_resumenes.ps1
#>
param(
    [string]$Server    = 'localhost\SQLEXPRESS',
    [string]$Database  = 'EnerGrid',
    [string]$Origen    = 'manual',                   # 'TaskScheduler' lo pasa explicitamente la tarea (registrar_tarea_refresco.ps1)
    [int]   $TimeoutSeg = 1800,                      # el refresco dura ~3 min; margen amplio
    [string]$LogDir    = (Join-Path $PSScriptRoot '..\logs')
)

$Origen = ($Origen -replace '[^A-Za-z0-9_ \-]', '').Trim()      # va dentro de un literal SQL: solo caracteres seguros
if (-not $Origen) { $Origen = 'manual' }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ('refresco_{0:yyyyMM}.log' -f (Get-Date))
function Write-Log([string]$m) {
    $l = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $m
    Add-Content -Path $log -Value $l -Encoding UTF8
    Write-Host $l
}

$sqlcmd = Get-Command sqlcmd.exe -ErrorAction SilentlyContinue
if (-not $sqlcmd) {
    Write-Log 'ERROR: sqlcmd.exe no esta en el PATH. Instala las utilidades de linea de comandos de SQL Server (sqlcmd).'
    exit 3
}

Write-Log "INICIO refresco (origen=$Origen servidor=$Server bd=$Database)"
$consulta = "SET NOCOUNT ON; EXEC energrid.usp_refrescar_resumenes_log @origen = N'$Origen';"

$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'            # stderr de sqlcmd no debe abortar el script; se decide por el codigo de salida
$salida = & $sqlcmd.Source -S $Server -d $Database -E -b -l 30 -t $TimeoutSeg -Q $consulta 2>&1
$codigo = $LASTEXITCODE
$ErrorActionPreference = $prev

foreach ($linea in @($salida)) { if ("$linea".Trim()) { Write-Log "sqlcmd: $linea" } }

if ($codigo -ne 0) {
    Write-Log "FIN con ERROR (codigo $codigo). Detalle en energrid.aud_refresco_log (columna error_mensaje)."
    exit $codigo
}
Write-Log 'FIN OK'
exit 0
