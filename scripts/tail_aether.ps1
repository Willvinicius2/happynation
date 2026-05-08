# Acompanha logs do stack Aether dentro do terminal integrado do VSCode.
#
# Uso:
#   .\scripts\tail_aether.ps1 server          # tail -f no log do server
#   .\scripts\tail_aether.ps1 validator       # tail no validator
#   .\scripts\tail_aether.ps1 -All            # multiplexa stdout de todos os logs
#   .\scripts\tail_aether.ps1 -List           # lista logs disponiveis
#
# Sai com Ctrl+C.

param(
    [Parameter(Position=0)][string]$Name,
    [switch]$All,
    [switch]$List,
    [int]$Lines = 60
)

$root = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $root "logs"

if (-not (Test-Path $logsDir)) {
    Write-Host "Sem logs ainda. Rode .\scripts\run_aether_bg.ps1 antes." -ForegroundColor Yellow
    exit 0
}

if ($List -or (-not $Name -and -not $All)) {
    Write-Host "Logs disponiveis em $logsDir:" -ForegroundColor Cyan
    Get-ChildItem $logsDir -Filter "*.log" | Sort-Object Name | ForEach-Object {
        $size = [math]::Round($_.Length / 1KB, 1)
        Write-Host ("  {0,-22} {1,8} KB  {2}" -f $_.Name, $size, $_.LastWriteTime)
    }
    if (-not $List) {
        Write-Host ""
        Write-Host "Uso: .\scripts\tail_aether.ps1 <nome>  (server, validator, critic, hyper, deploy)" -ForegroundColor DarkGray
        Write-Host "     .\scripts\tail_aether.ps1 -All   (multiplexa todos)" -ForegroundColor DarkGray
    }
    exit 0
}

if ($All) {
    $logs = Get-ChildItem $logsDir -Filter "*.log" | Where-Object { $_.Name -notmatch "\.err\.log$" }
    Write-Host ("Tailing {0} log(s). Ctrl+C para sair." -f $logs.Count) -ForegroundColor Cyan
    # Get-Content -Wait so segura 1 arquivo. Multiplexa via Get-EventSubscriber? Mais simples: poll loop.
    $positions = @{}
    foreach ($f in $logs) { $positions[$f.FullName] = (Get-Item $f.FullName).Length }
    Write-Host ""
    while ($true) {
        foreach ($f in $logs) {
            $cur = (Get-Item $f.FullName).Length
            $last = $positions[$f.FullName]
            if ($cur -gt $last) {
                $stream = [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
                $null = $stream.Seek($last, 'Begin')
                $reader = New-Object System.IO.StreamReader($stream)
                $tag = ($f.BaseName).PadRight(10)
                while (-not $reader.EndOfStream) {
                    $line = $reader.ReadLine()
                    if ($line) { Write-Host ("[{0}] {1}" -f $tag, $line) }
                }
                $reader.Close(); $stream.Close()
                $positions[$f.FullName] = $cur
            }
        }
        Start-Sleep -Milliseconds 500
    }
    exit 0
}

$path = Join-Path $logsDir ("{0}.log" -f $Name)
if (-not (Test-Path $path)) {
    Write-Host ("Log nao encontrado: {0}" -f $path) -ForegroundColor Red
    Write-Host "Tente -List para ver os disponiveis." -ForegroundColor DarkGray
    exit 1
}

Write-Host ("Tail -f {0} (ultimas {1} linhas + segue)" -f $path, $Lines) -ForegroundColor Cyan
Write-Host ""
Get-Content -Path $path -Tail $Lines -Wait
