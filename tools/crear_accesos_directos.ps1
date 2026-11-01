# Creates the "Registro de Mantencion" shortcuts (Desktop and Start menu) that open the app like any program.
# usage: powershell -ExecutionPolicy Bypass -File tools\crear_accesos_directos.ps1 [-Quitar]
param([switch]$Quitar)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$name = "Registro de Mantenci$([char]0x00F3)n.lnk"   # written this way so the script stays ASCII
$targets = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))

foreach ($dir in $targets) {
    $path = Join-Path $dir $name
    if ($Quitar) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path; "Quitado: $path" }
        continue
    }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe'
    $lnk.Arguments = '"' + (Join-Path $root 'launcher.pyw') + '"'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = (Join-Path $root 'app\static\icon.ico') + ',0'
    $lnk.Description = "Registro de mantenci$([char]0x00F3)n por voz - CAT 994K"
    $lnk.Save()
    "Creado: $path"
}
