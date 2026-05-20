# Vigia simples: checa processos do stack a cada 60s, loga em logs/vigia.log
$ErrorActionPreference = "SilentlyContinue"
$root = "c:\Users\wvini\OneDrive\Área de Trabalho\openclaude\HAPPYNATIONBOT"
$log  = Join-Path $root "logs\vigia.log"
function Stamp { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }
"[$(Stamp)] vigia iniciado" | Out-File -Append -Encoding utf8 $log
while ($true) {
    $lines = @()
    foreach ($f in @("logs\aether.pids","logs\happynation.pids")) {
        $p = Join-Path $root $f
        if (Test-Path $p) {
            foreach ($l in Get-Content $p) {
                $parts = $l -split "\s+"
                $pid_ = $parts[0]; $name = $parts[1]
                $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
                if (-not $proc) { $lines += "DEAD $name (pid $pid_)" }
            }
        }
    }
    if ($lines.Count -gt 0) {
        "[$(Stamp)] ALERTA: " + ($lines -join "; ") | Out-File -Append -Encoding utf8 $log
    } else {
        "[$(Stamp)] OK - 7 processos vivos" | Out-File -Append -Encoding utf8 $log
    }
    Start-Sleep -Seconds 60
}
