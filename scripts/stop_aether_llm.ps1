# Para APENAS os modulos do Aether que dependem de Claude/LLM:
#   server, auto_validator, hyper_agent
# NAO toca em critic_agent, promote_loop, ou bot principal — esses
# rodam 24/7 sem IA.

$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'src\.research\.aether\.(server|auto_validator|hyper_agent)'
} | ForEach-Object {
    $mod = if ($_.CommandLine -match 'src\.research\.aether\.(\w+)') { $matches[1] } else { '?' }
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "killed $mod pid=$($_.ProcessId)"
    $script:killed++
}

if ($killed -eq 0) {
    Write-Host "Nenhum processo LLM rodando."
}
