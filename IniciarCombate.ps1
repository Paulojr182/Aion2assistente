param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{32}$')][string]$Token)
$ErrorActionPreference='Stop'
$clients=@(Get-Process -Name AION2 -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
if ($clients.Count -ne 1) { throw 'Abra uma única janela do AION 2 e entre no personagem.' }
$gameProcess=$clients[0]
# Mesmo Python que abriu o painel (embutido no pacote ou instalado)
$pythonExe=$env:AION_BOT_PYTHON
if (-not $pythonExe -or -not (Test-Path $pythonExe)) { $pythonExe=Join-Path $PSScriptRoot 'python\python.exe' }
if (-not (Test-Path $pythonExe)) { $pythonExe=Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe' }
if (-not (Test-Path $pythonExe)) { throw 'Python 3.12 nao encontrado. Veja o LEIA-ME.' }
$server=Join-Path $PSScriptRoot 'combat_server.py'
$arguments='-X utf8 -B "'+$server+'" --pid '+$gameProcess.Id+' --window '+$gameProcess.MainWindowHandle.ToInt64()+' --token '+$Token
Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -Verb RunAs -WindowStyle Hidden | Out-Null
