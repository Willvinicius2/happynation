# HappyNationBot Operating Memory

## Aether Research Priority

- Keep Aether running and researching continuously. Do not disable Aether research sidecars as a convenience.
- If a restart is unavoidable, make it a short restart and immediately bring Aether back with the full research stack: server, auto_validator, critic_agent, hyper_agent, auto_deploy, continuous cycle, and deploy watchdog.
- Prefer fixing stuck calls, stale logs, duplicate processes, or bad launch configuration without taking the research loop offline.
- The goal is to extract the maximum number of winning strategies from Aether while keeping deploy gates strict enough to avoid weak strategies entering paper trading.
- Do not confuse the disabled `promote_loop` with Aether research. `promote_loop` stays off unless explicitly requested because it has caused global kill-switch/liquidation risk; Aether research itself should remain active.
