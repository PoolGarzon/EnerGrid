<#
.SYNOPSIS
  Registra (o quita) la tarea programada de Windows que refresca los resumenes de EnerGrid. POR DEFECTO NO HACE NINGUN CAMBIO.

.DESCRIPTION
  -Accion Mostrar   (por defecto) imprime lo que se crearia. No cambia nada.
  -Accion Instalar  crea la tarea SOLO si ademas se pasa -Confirmar.  Sin -Confirmar solo muestra un aviso.
  -Accion Quitar    elimina la tarea SOLO si ademas se pasa -Confirmar.
  -Accion Estado    muestra ultima/proxima ejecucion y resultado de la tarea (solo lectura).

  Requisitos antes de instalar:
    1) Haber ejecutado sql\04_procedimientos.sql y sql\05_resumenes_sla.sql (crean energrid.usp_refrescar_resumenes_log y sus tablas).
    2) La cuenta de Windows que ejecuta la tarea debe tener permiso en SQL Server para EXEC del procedimiento y para crear/borrar/renombrar
       tablas del esquema energrid (db_owner o db_ddladmin + EXECUTE), porque usp_refrescar_resumenes reconstruye las tablas mat_*.
    3) sqlcmd.exe en el PATH.
  La tarea se registra como S4U (se ejecuta con o sin sesion abierta, sin guardar contrasena). Si SQL Server rechaza la conexion con ese
  tipo de inicio de sesion, vuelve a registrarla con -Interactiva (solo corre con la sesion de Windows abierta).

  -Accion Actualizar cambia SOLO el disparador de la tarea EXISTENTE (no crea otra, no toca la accion, la cuenta ni los ajustes) a la
  hora indicada en -Hora (por defecto 08:10, todos los dias). Requiere -Confirmar; sin el solo muestra el cambio previsto.
  Al terminar verifica la tarea y muestra la proxima ejecucion. Si la tarea no existe, falla y pide usar Instalar.
  Instalar NO sobrescribe una tarea existente: si ya existe, indica usar Actualizar.

.EXAMPLE
  .\registrar_tarea_refresco.ps1                                        # vista previa
  .\registrar_tarea_refresco.ps1 -Accion Actualizar                     # vista previa del cambio de horario (08:10)
  .\registrar_tarea_refresco.ps1 -Accion Actualizar -Hora 08:10 -Confirmar   # cambia el disparador de la tarea existente
  .\registrar_tarea_refresco.ps1 -Accion Instalar -Hora 08:10 -Confirmar     # crea la tarea (solo si no existe)
  .\registrar_tarea_refresco.ps1 -Accion Estado
#>
param(
    [ValidateSet('Mostrar', 'Instalar', 'Quitar', 'Estado', 'Actualizar')][string]$Accion = 'Mostrar',
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$Hora = '08:10',
    [string]$NombreTarea = 'EnerGrid - Refresco de resumenes',
    [string]$Server = 'localhost\SQLEXPRESS',
    [string]$Database = 'EnerGrid',
    [switch]$Interactiva,
    [switch]$Confirmar
)

$ps1 = Join-Path $PSScriptRoot 'refrescar_resumenes.ps1'
$proyecto = Split-Path -Parent $PSScriptRoot
$argumentos = "-NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -Server `"$Server`" -Database `"$Database`" -Origen TaskScheduler"
$usuario = "$env:USERDOMAIN\$env:USERNAME"
$logon = if ($Interactiva) { 'Interactive' } else { 'S4U' }

function Mostrar-Plan {
    Write-Host "Tarea:      $NombreTarea"
    Write-Host "Frecuencia: diaria a las $Hora (hora local del equipo); si el equipo estaba apagado, se ejecuta al encender (StartWhenAvailable)"
    Write-Host "Comando:    powershell.exe $argumentos"
    Write-Host "Cuenta:     $usuario  (inicio de sesion: $logon)"
    Write-Host "Limites:    maximo 1 h por ejecucion; no se solapa; 2 reintentos cada 10 min si falla"
    Write-Host "Log:        $proyecto\logs\refresco_AAAAMM.log  +  energrid.aud_refresco_log"
}

# Horas de los disparadores de una tarea (HH:mm) para mostrarlas y compararlas.
function Get-HorasDisparador($tarea) {
    @($tarea.Triggers | ForEach-Object {
        if ($_.StartBoundary) { ([datetime]::Parse($_.StartBoundary, [Globalization.CultureInfo]::InvariantCulture)).ToString('HH:mm') } else { '(sin hora)' }
    })
}

switch ($Accion) {
    'Mostrar' { Mostrar-Plan; Write-Host "`n(Vista previa: no se ha creado nada.)" }
    'Estado' {
        $t = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "La tarea '$NombreTarea' no existe."; break }
        $i = Get-ScheduledTaskInfo -TaskName $NombreTarea
        [pscustomobject]@{ Estado = $t.State; Disparador = (Get-HorasDisparador $t) -join ', '; UltimaEjecucion = $i.LastRunTime; UltimoResultado = $i.LastTaskResult; Proxima = $i.NextRunTime } | Format-List
    }
    'Instalar' {
        Mostrar-Plan
        if (-not $Confirmar) { Write-Host "`nNO se creo la tarea. Para crearla (requiere autorizacion) vuelve a ejecutar con -Confirmar."; break }
        if (-not (Test-Path $ps1)) { throw "No existe $ps1" }
        if (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue) {
            throw "La tarea '$NombreTarea' ya existe. No se crea otra ni se sobrescribe. Para cambiar el horario usa: -Accion Actualizar -Hora $Hora -Confirmar"
        }
        $accionT = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argumentos -WorkingDirectory $proyecto
        $trigger = New-ScheduledTaskTrigger -Daily -At $Hora
        $ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
                   -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
        $princ = New-ScheduledTaskPrincipal -UserId $usuario -LogonType $logon -RunLevel Limited
        Register-ScheduledTask -TaskName $NombreTarea -Action $accionT -Trigger $trigger -Settings $ajustes -Principal $princ `
            -Description 'Refresca los resumenes mat_* de EnerGrid (EXEC energrid.usp_refrescar_resumenes_log). No ejecuta el ETL.' -ErrorAction Stop | Out-Null
        if (-not (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue)) { throw "No se pudo verificar la creacion de la tarea '$NombreTarea'." }
        Write-Host "`nTarea '$NombreTarea' creada y verificada. Prueba inmediata (opcional): Start-ScheduledTask -TaskName '$NombreTarea'"
    }
    'Actualizar' {
        $t = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
        if (-not $t) { throw "La tarea '$NombreTarea' no existe. No se crea nada. Para crearla usa: -Accion Instalar -Hora $Hora -Confirmar" }
        $antes = [pscustomobject]@{
            Horas     = (Get-HorasDisparador $t) -join ', '
            Argumento = ($t.Actions | Select-Object -First 1).Arguments
            Usuario   = $t.Principal.UserId
            Logon     = "$($t.Principal.LogonType)"
        }
        Write-Host "Tarea:             $NombreTarea"
        Write-Host "Disparador actual: diario a las $($antes.Horas)"
        Write-Host "Disparador nuevo:  diario a las $Hora (todos los dias; hora local del equipo)"
        Write-Host "Se cambia SOLO el disparador. No se toca la accion, la cuenta ni los ajustes (StartWhenAvailable, IgnoreNew, 2 reintentos cada 10 min, limite de 1 h)."
        if (-not $Confirmar) { Write-Host "`nNO se cambio nada. Para aplicar el cambio vuelve a ejecutar con -Confirmar."; break }

        $trigger = New-ScheduledTaskTrigger -Daily -At $Hora
        Set-ScheduledTask -TaskName $NombreTarea -Trigger $trigger -ErrorAction Stop | Out-Null

        # Verificacion posterior: se vuelve a leer la tarea desde el Programador (no se da por hecho el cambio).
        $d = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction Stop
        $horas = Get-HorasDisparador $d
        $s = $d.Settings
        $fallos = @()
        if (@($d.Triggers).Count -ne 1 -or $horas -notcontains $Hora) { $fallos += "El disparador no quedo a las $Hora (hay: $($horas -join ', '))." }
        if ($d.Triggers[0].DaysInterval -ne 1) { $fallos += "El disparador no es diario cada 1 dia (DaysInterval=$($d.Triggers[0].DaysInterval))." }
        if (-not $s.StartWhenAvailable) { $fallos += 'StartWhenAvailable no esta activo.' }
        if ("$($s.MultipleInstances)" -ne 'IgnoreNew') { $fallos += "MultipleInstances = $($s.MultipleInstances) (se esperaba IgnoreNew)." }
        if ($s.RestartCount -ne 2) { $fallos += "RestartCount = $($s.RestartCount) (se esperaba 2)." }
        if ("$($s.RestartInterval)" -ne 'PT10M') { $fallos += "RestartInterval = $($s.RestartInterval) (se esperaba PT10M)." }
        if ("$($s.ExecutionTimeLimit)" -ne 'PT1H') { $fallos += "ExecutionTimeLimit = $($s.ExecutionTimeLimit) (se esperaba PT1H)." }
        if ((($d.Actions | Select-Object -First 1).Arguments) -ne $antes.Argumento) { $fallos += 'La accion (argumentos) cambio.' }
        if ($d.Principal.UserId -ne $antes.Usuario -or "$($d.Principal.LogonType)" -ne $antes.Logon) { $fallos += 'La cuenta o el tipo de inicio de sesion cambio.' }
        foreach ($f in $fallos) { Write-Warning $f }

        $i = Get-ScheduledTaskInfo -TaskName $NombreTarea
        if ($i.NextRunTime -and $i.NextRunTime.ToString('HH:mm') -ne $Hora) { Write-Warning "La proxima ejecucion ($($i.NextRunTime)) no coincide con las $Hora." }
        if ($fallos.Count -eq 0) { Write-Host "`nTarea '$NombreTarea' actualizada y verificada: diaria a las $Hora." }
        else { Write-Host "`nTarea '$NombreTarea' actualizada, pero la verificacion encontro diferencias (ver advertencias arriba)." }
        Write-Host "Proxima ejecucion: $($i.NextRunTime)"
        Write-Host "Ultima ejecucion:  $($i.LastRunTime)  (resultado $($i.LastTaskResult))"
    }
    'Quitar' {
        if (-not $Confirmar) { Write-Host "NO se elimino nada. Para quitar la tarea vuelve a ejecutar con -Confirmar."; break }
        Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false -ErrorAction Stop
        Write-Host "Tarea '$NombreTarea' eliminada."
    }
}
