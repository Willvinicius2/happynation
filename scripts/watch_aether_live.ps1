# Vigia ao vivo: emite uma linha (= um evento) para cada acontecimento
# relevante no stack do Aether + bot de paper.
#
# Filtra agressivamente: so emite eventos acionaveis (validations,
# approvals/rejections, deploys, mudancas no manifest, novos trades,
# erros). Stream silencioso quando nada interessante acontece.

$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding

$root = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $root "logs"
$paperState = Join-Path $root "src\logs\paper_balance\state.json"
$manifest = Join-Path $root "src\logs\research\active_strategies.json"

function Get-LatestLog([string]$prefix) {
    return Get-ChildItem (Join-Path $logsDir "$prefix-*.log") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

function Now { (Get-Date).ToString("HH:mm:ss") }

# Estado: posicao (size) por arquivo, snapshots anteriores
$pos = @{}
$lastManifestCount = $null
$lastTradeCount = $null
$lastBalance = $null
$lastDeployCandidates = $null
$lastDeployTuple = $null
$lastTier1 = 0
$lastTier2 = 0
$lastSummaryAt = (Get-Date).AddMinutes(-99)
$pendingManifestCount = $null

# Tags + filtros relevantes por modulo
$watchers = @(
    @{ name = "validator"; prefix = "validator"; pattern = "(validated id=|ciclo:|ERROR|Exception|Traceback)" },
    @{ name = "critic";    prefix = "critic";    pattern = "(approved id=|rejected id=|ciclo:|ERROR|Exception|Traceback)" },
    @{ name = "deploy";    prefix = "deploy";    pattern = "(ERROR|Exception|Traceback)" },
    @{ name = "hyper";     prefix = "hyper";     pattern = "(controls ->|ERROR|Exception|Traceback)" },
    @{ name = "bot";       prefix = "bot";       pattern = "(BUY|SELL|circuit breaker|risk_guard|ERROR|Exception|Traceback)" }
)

# Inicializa posicoes (skip historico, so eventos novos)
foreach ($w in $watchers) {
    $f = Get-LatestLog $w.prefix
    if ($f) { $pos["$($w.name)|$($f.FullName)"] = $f.Length }
}
if (Test-Path $manifest) {
    try {
        $j = Get-Content $manifest -Raw | ConvertFrom-Json
        $lastManifestCount = $j.strategies.Count
    } catch {}
}
if (Test-Path $paperState) {
    try {
        $j = Get-Content $paperState -Raw | ConvertFrom-Json
        $lastBalance = [double]$j._meta.equity
        $stats = $j._meta.strategy_stats
        $lastTradeCount = ($stats.PSObject.Properties | ForEach-Object { [int]$_.Value.trades } | Measure-Object -Sum).Sum
    } catch {}
}

Write-Host "[$(Now)] [vigia] watcher armado - manifest=$lastManifestCount banca=$lastBalance trades=$lastTradeCount"

while ($true) {
    foreach ($w in $watchers) {
        $f = Get-LatestLog $w.prefix
        if (-not $f) { continue }
        $key = "$($w.name)|$($f.FullName)"
        if (-not $pos.ContainsKey($key)) { $pos[$key] = 0 }
        $size = $f.Length
        if ($size -gt $pos[$key]) {
            try {
                $fs = [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
                $fs.Position = $pos[$key]
                $sr = New-Object System.IO.StreamReader($fs)
                $chunk = $sr.ReadToEnd()
                $sr.Close()
                $fs.Close()
                foreach ($line in ($chunk -split "`n")) {
                    $line = $line.Trim()
                    if (-not $line) { continue }
                    if ($line -match $w.pattern) {
                        # Compacta linha: remove timestamp do log se tiver
                        $clean = $line -replace '^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,\.]\d+\s+(INFO|WARNING|ERROR|DEBUG)\s+', ''
                        Write-Host "[$(Now)] [$($w.name)] $clean"
                    }
                }
            } catch {}
            $pos[$key] = $size
        } elseif ($size -lt $pos[$key]) {
            # Log foi truncado/rotacionado
            $pos[$key] = $size
        }
    }

    # Mudancas no auto_deploy: candidates aprovadas (nova validada+critic)
    $deployLog = Get-LatestLog "deploy"
    if ($deployLog) {
        try {
            $lastLine = (Get-Content $deployLog.FullName -Tail 1 -ErrorAction SilentlyContinue)
            if ($lastLine -and $lastLine -match 'candidates=(\d+).*deployed=(\d+).*removed_stale=(\d+)') {
                $cands = [int]$matches[1]
                $dep = [int]$matches[2]
                $stale = [int]$matches[3]
                $tuple = "$cands|$dep|$stale|$($deployLog.LastWriteTime.Ticks)"
                # Skip se mesma linha (mesmo ciclo) - evita re-emitir
                if ($tuple -ne $lastDeployTuple) {
                    if ($null -ne $lastDeployCandidates -and $cands -ne $lastDeployCandidates) {
                        $delta = $cands - $lastDeployCandidates
                        $sign = if ($delta -gt 0) { "+$delta" } else { "$delta" }
                        Write-Host "[$(Now)] [deploy] candidates: $lastDeployCandidates -> $cands ($sign) | deployed=$dep stale=$stale"
                    }
                    if ($stale -gt 0) {
                        Write-Host "[$(Now)] [deploy] removed_stale=$stale (rebalanceamento no manifest)"
                    }
                    $lastDeployCandidates = $cands
                    $lastDeployTuple = $tuple
                }
            }
        } catch {}
    }

    # Mudancas no manifest
    if (Test-Path $manifest) {
        try {
            $j = Get-Content $manifest -Raw | ConvertFrom-Json
            $cur = $j.strategies.Count
            # Auto_deploy reescreve o arquivo a cada 60s; durante a escrita o
            # parse pode retornar count=0 transiente. Quedas suspeitas (count=0
            # ou >50%) sao confirmadas com 2 leituras consecutivas iguais.
            $suspicious = ($cur -eq 0 -and $lastManifestCount -gt 0) -or `
                          ($lastManifestCount -gt 10 -and $cur -lt ($lastManifestCount * 0.5))
            $emit = $false
            if ($suspicious) {
                if ($pendingManifestCount -eq $cur) {
                    $emit = $true
                    $pendingManifestCount = $null
                } else {
                    $pendingManifestCount = $cur
                }
            } else {
                $pendingManifestCount = $null
                if ($null -ne $lastManifestCount -and $cur -ne $lastManifestCount) { $emit = $true }
            }
            if ($emit) {
                $delta = $cur - $lastManifestCount
                $sign = if ($delta -gt 0) { "+$delta" } else { "$delta" }
                Write-Host "[$(Now)] [manifest] estrategias ativas: $lastManifestCount -> $cur ($sign)"
                $lastManifestCount = $cur
            }
        } catch {}
    }

    # Mudancas no paper (trades novos, mudanca de saldo, tier 1/2 emergindo)
    if (Test-Path $paperState) {
        try {
            $j = Get-Content $paperState -Raw | ConvertFrom-Json
            $bal = [double]$j._meta.equity
            $stats = $j._meta.strategy_stats
            $tradeCount = ($stats.PSObject.Properties | ForEach-Object { [int]$_.Value.trades } | Measure-Object -Sum).Sum

            if ($null -ne $lastTradeCount -and $tradeCount -gt $lastTradeCount) {
                $delta = $tradeCount - $lastTradeCount
                $blDelta = $bal - $lastBalance
                $blSign = if ($blDelta -ge 0) { "+" } else { "" }
                Write-Host ("[{0}] [paper] +{1} trade(s) | banca {2:N2} ({3}{4:N2}) | total trades={5}" -f (Now), $delta, $bal, $blSign, $blDelta, $tradeCount)
                $lastTradeCount = $tradeCount
                $lastBalance = $bal
            }

            # Conta estrategias com pnl>0 e trades>=5 (Tier 1/2 candidatas)
            $eligible = @($stats.PSObject.Properties | Where-Object {
                [int]$_.Value.trades -ge 5 -and [double]$_.Value.pnl_usd -gt 0
            })
            $tier1 = if ($eligible.Count -ge 1) { 1 } else { 0 }
            $tier2 = [Math]::Min(4, [Math]::Max(0, $eligible.Count - 1))
            if ($tier1 -ne $lastTier1 -or $tier2 -ne $lastTier2) {
                Write-Host "[$(Now)] [tier] mudanca: T1=$tier1 T2=$tier2 (eligiveis com >=5 trades e PnL>0: $($eligible.Count))"
                $lastTier1 = $tier1
                $lastTier2 = $tier2
            }
        } catch {}
    }

    # Resumo periodico (a cada 5 min) - mostra que o watcher continua vivo
    if (((Get-Date) - $lastSummaryAt).TotalMinutes -ge 5) {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'src\.research\.aether|src\.main|src\.dashboard|src\.agent_runner' }).Count
        Write-Host ("[{0}] [vigia] heartbeat: {1} pyprocs | manifest={2} | banca={3:N2} | trades={4}" -f (Now), $procs, $lastManifestCount, $lastBalance, $lastTradeCount)
        $lastSummaryAt = Get-Date
    }

    Start-Sleep -Seconds 8
}
