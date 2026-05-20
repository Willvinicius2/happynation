import json

from src.agents.aether_researcher_agent import AetherResearcherAgent
from src.agents.base import AgentFinding, AgentSeverity
from datetime import datetime, timedelta, timezone

from src.agents.config_agent import ConfigAgent
from src.agents.data_quality_agent import DataQualityAgent
from src.agents.drift_agent import DriftAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.health_agent import HealthAgent
from src.agents.market_regime_agent import MarketRegimeAgent
from src.agents.performance_agent import PerformanceAgent
from src.agents.policy_agent import PolicyAgent
from src.agents.portfolio_agent import PortfolioAgent
from src.agents.research_agent import ResearchAgent
from src.agents.risk_agent import RiskAgent
from src.agents.runner import AgentOrchestrator
from src.agents.security_agent import SecurityAgent
from src.agents.strategy_researcher_agent import StrategyResearcherAgent
from src.agents.strategy_validator_agent import StrategyValidatorAgent


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_risk_agent_flags_drawdown_and_loss_streak(tmp_path):
    state_path = tmp_path / "paper" / "state.json"
    _write_json(
        state_path,
        {
            "BTCUSDT": {
                "strategy": "Test",
                "balance": 90.0,
                "initial_balance": 100.0,
                "total_trades": 2,
                "wins": 0,
                "losses": 2,
                "position": None,
                "risk": {
                    "day_start_balance": 100.0,
                    "daily_realized_pnl": -2.0,
                    "consecutive_losses": 2,
                    "last_block_reason": "",
                },
            }
        },
    )

    findings = RiskAgent(
        state_path=state_path,
        max_daily_loss_pct=1.0,
        max_drawdown_pct=5.0,
        max_consecutive_losses=2,
    ).run()

    titles = {finding.title for finding in findings}
    assert "Perda diaria acima do limite" in titles
    assert "Drawdown acima do limite" in titles
    assert "Sequencia de perdas detectada" in titles


def test_performance_agent_flags_negative_asset(tmp_path):
    state_path = tmp_path / "paper" / "state.json"
    _write_json(
        state_path,
        {
            "ETHUSDT": {
                "strategy": "Test",
                "balance": 95.0,
                "initial_balance": 100.0,
                "total_trades": 4,
                "wins": 1,
                "losses": 3,
                "position": None,
                "risk": {},
            }
        },
    )

    findings = PerformanceAgent(state_path=state_path, min_trades=3, min_winrate_pct=40.0).run()

    titles = {finding.title for finding in findings}
    assert "Win rate abaixo do minimo" in titles
    assert "PnL acumulado negativo" in titles


def test_research_agent_recommends_deploy_when_catalog_has_entries(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(catalog_path, {"entries": [{"id": "abc", "approved": True}]})

    findings = ResearchAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].title == "Sem estrategias ativas suficientes"
    assert "Deployar" in findings[0].action


def test_research_agent_counts_catalog_dict_entries(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(
        catalog_path,
        {
            "entries": {
                "approved-id": {"id": "approved-id", "status": "approved"},
                "rejected-id": {"id": "rejected-id", "status": "rejected"},
            }
        },
    )

    findings = ResearchAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].metadata["approved_count"] == 1
    assert "Deployar" in findings[0].action


def test_research_agent_ignores_rejected_catalog_entries(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(catalog_path, {"entries": {"rejected-id": {"status": "rejected"}}})

    findings = ResearchAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].metadata["approved_count"] == 0
    assert "Rodar discovery" in findings[0].action


def test_research_agent_counts_aether_active_approved_entries(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(catalog_path, {"entries": {}})
    _write_json(
        active_path,
        {
            "strategies": [
                {
                    "id": "aether-ok",
                    "source": "aether_auto_deploy",
                    "summary": {
                        "critic_status": "approved",
                        "validation_passed": True,
                    },
                }
            ]
        },
    )

    findings = ResearchAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].title == "Research com manifest ativo"
    assert findings[0].metadata["active_approved_count"] == 1
    assert findings[0].metadata["approved_count"] == 1


def test_orchestrator_writes_report(tmp_path):
    class EmptyAgent:
        name = "empty"

        def run(self):
            return []

    report_path = tmp_path / "agents" / "latest_report.json"
    report = AgentOrchestrator([EmptyAgent()], report_path=report_path).run()

    assert report_path.exists()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["generated_at"] == report.generated_at


def test_orchestrator_marks_new_persistent_and_resolved_findings(tmp_path):
    class OneFindingAgent:
        name = "one"

        def __init__(self, metric):
            self.metric = metric

        def run(self):
            return [
                AgentFinding(
                    agent=self.name,
                    severity=AgentSeverity.WARNING,
                    title="Mesmo problema",
                    message=f"Metrica atual: {self.metric}",
                    asset="BTCUSDT",
                    metadata={"metric": self.metric},
                )
            ]

    class EmptyAgent:
        name = "empty"

        def run(self):
            return []

    report_path = tmp_path / "agents" / "latest_report.json"

    first_report = AgentOrchestrator([OneFindingAgent(10)], report_path=report_path).run()
    assert first_report.findings[0].status == "new"
    first_seen_at = first_report.findings[0].first_seen_at
    first_fingerprint = first_report.findings[0].fingerprint()

    second_report = AgentOrchestrator([OneFindingAgent(20)], report_path=report_path).run()
    assert second_report.findings[0].status == "persistent"
    assert second_report.findings[0].first_seen_at == first_seen_at
    assert second_report.findings[0].fingerprint() == first_fingerprint

    third_report = AgentOrchestrator([EmptyAgent()], report_path=report_path).run()
    assert third_report.findings == []
    assert len(third_report.resolved_findings) == 1
    assert third_report.resolved_findings[0].status == "resolved"
    assert third_report.resolved_findings[0].first_seen_at == first_seen_at


def test_report_json_includes_diff_counts(tmp_path):
    class WarningAgent:
        name = "warning"

        def run(self):
            return [
                AgentFinding(
                    agent=self.name,
                    severity=AgentSeverity.WARNING,
                    title="Alerta",
                    message="Teste",
                )
            ]

    report_path = tmp_path / "agents" / "latest_report.json"
    AgentOrchestrator([WarningAgent()], report_path=report_path).run()
    data = json.loads(report_path.read_text(encoding="utf-8"))

    assert data["diff_counts"] == {"new": 1, "persistent": 0, "resolved": 0}
    assert data["findings"][0]["first_seen_at"]
    assert data["findings"][0]["last_seen_at"]
    assert data["findings"][0]["status"] == "new"
    assert data["findings"][0]["fingerprint"]


def test_fingerprint_distinguishes_identity_metadata():
    first = AgentFinding(
        agent="health_agent",
        severity=AgentSeverity.WARNING,
        title="Log acima do limite",
        message="log 1",
        metadata={"path": "src/logs/a.log", "size_mb": 60.0},
    )
    second = AgentFinding(
        agent="health_agent",
        severity=AgentSeverity.WARNING,
        title="Log acima do limite",
        message="log 2",
        metadata={"path": "src/logs/b.log", "size_mb": 61.0},
    )
    changed_metric_same_path = AgentFinding(
        agent="health_agent",
        severity=AgentSeverity.WARNING,
        title="Log acima do limite",
        message="log 1 changed",
        metadata={"path": "src/logs/a.log", "size_mb": 70.0},
    )

    assert first.fingerprint() != second.fingerprint()
    assert first.fingerprint() == changed_metric_same_path.fingerprint()


def _write_env(path, lines):
    path.write_text("\n".join(lines), encoding="utf-8")


def test_config_agent_flags_real_mode_without_confirmation(tmp_path):
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            'BINANCE_API_KEY="abc123ABC123abc123ABC123"',
            'BINANCE_SECRET_KEY="def456DEF456def456DEF456"',
            'TELEGRAM_BOT_TOKEN="111:abc"',
            'TELEGRAM_CHAT_ID="123"',
            'PAPER_TRADING="false"',
            'MAX_DAILY_LOSS_PCT="1.25"',
            'MAX_ASSET_DRAWDOWN_PCT="3.0"',
            'MAX_CONSECUTIVE_LOSSES="2"',
        ],
    )

    findings = ConfigAgent(env_path=env_path).run()
    titles = {f.title for f in findings}
    assert "Modo real sem confirmacao" in titles


def test_config_agent_flags_missing_required_keys(tmp_path):
    env_path = tmp_path / ".env"
    _write_env(env_path, ['PAPER_TRADING="true"'])

    findings = ConfigAgent(env_path=env_path).run()
    titles = {f.title for f in findings}
    assert "Variavel obrigatoria ausente" in titles


def test_config_agent_passes_when_paper_and_complete(tmp_path):
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            'BINANCE_API_KEY="abcXYZ123abcXYZ123abc"',
            'BINANCE_SECRET_KEY="abcXYZ123abcXYZ123abc"',
            'TELEGRAM_BOT_TOKEN="111:abc"',
            'TELEGRAM_CHAT_ID="123"',
            'PAPER_TRADING="true"',
        ],
    )

    findings = ConfigAgent(env_path=env_path).run()
    assert any(f.title == "Configuracao valida" for f in findings)


def test_security_agent_flags_missing_gitignore_entry(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.log\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text('PAPER_TRADING="true"\n', encoding="utf-8")

    findings = SecurityAgent(repo_root=tmp_path, env_path=env_path, gitignore_path=gitignore).run()
    titles = {f.title for f in findings}
    assert ".env nao esta no .gitignore" in titles


def test_security_agent_clean_when_protected(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".env\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text('PAPER_TRADING="true"\n', encoding="utf-8")

    findings = SecurityAgent(repo_root=tmp_path, env_path=env_path, gitignore_path=gitignore).run()
    assert findings[0].title == "Cofre sem alertas"


def test_health_agent_flags_corrupted_state_file(tmp_path):
    state_dir = tmp_path / "src" / "logs" / "paper_balance"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{not valid json", encoding="utf-8")

    findings = HealthAgent(repo_root=tmp_path).run()
    titles = {f.title for f in findings}
    assert "Arquivo de estado corrompido" in titles


def test_health_agent_flags_large_log(tmp_path):
    log_dir = tmp_path / "src" / "logs"
    log_dir.mkdir(parents=True)
    big_log = log_dir / "trading_bot.log"
    big_log.write_bytes(b"x" * (60 * 1024 * 1024))

    findings = HealthAgent(repo_root=tmp_path, log_size_warning_mb=50.0).run()
    titles = {f.title for f in findings}
    assert "Log acima do limite" in titles


def test_health_agent_clean_when_no_issues(tmp_path):
    findings = HealthAgent(repo_root=tmp_path).run()
    assert findings[0].title == "Saude OK"


def test_strategy_researcher_flags_empty_catalog(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    _write_json(catalog_path, {"entries": {}})

    findings = StrategyResearcherAgent(catalog_path=catalog_path).run()

    assert findings[0].title == "Catalogo de estrategias vazio"


def test_strategy_researcher_flags_saturated_template(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    entries = {
        f"id-{idx}": {
            "id": f"id-{idx}",
            "template": "RSI_Reversal",
            "status": "rejected",
            "origin": "grid",
        }
        for idx in range(5)
    }
    _write_json(
        catalog_path,
        {
            "updated_at": "2026-05-03T00:00:00Z",
            "rounds": 1,
            "entries": entries,
        },
    )

    findings = StrategyResearcherAgent(catalog_path=catalog_path, saturated_rejections=5).run()
    titles = {finding.title for finding in findings}

    assert "Catalogo sem estrategias aprovadas" in titles
    assert "Template saturado de rejeicoes" in titles


def test_strategy_validator_flags_active_without_approval(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(catalog_path, {"entries": {}})
    _write_json(active_path, {"strategies": [{"id": "live-id", "template": "RSI"}]})

    findings = StrategyValidatorAgent(catalog_path=catalog_path, active_path=active_path).run()
    titles = {finding.title for finding in findings}

    assert "Estrategia ativa nao aprovada" in titles


def test_strategy_validator_flags_approved_without_metrics(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(
        catalog_path,
        {
            "entries": {
                "approved-id": {
                    "id": "approved-id",
                    "template": "Trend",
                    "status": "approved",
                    "summary": {},
                }
            }
        },
    )
    _write_json(active_path, {"strategies": []})

    findings = StrategyValidatorAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].title == "Aprovada sem metricas suficientes"
    assert "total_trades" in findings[0].metadata["missing"]


def test_strategy_validator_flags_approved_that_violates_criteria(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(
        catalog_path,
        {
            "entries": {
                "weak-id": {
                    "id": "weak-id",
                    "template": "Trend",
                    "status": "approved",
                    "summary": {
                        "total_trades": 30,
                        "profit_factor": 1.02,
                        "max_drawdown_pct": 28.0,
                        "max_loss_streak": 3,
                        "consistency_score": 60.0,
                        "windows_passed": 3,
                        "windows_total": 5,
                    },
                }
            }
        },
    )
    _write_json(active_path, {"strategies": []})

    findings = StrategyValidatorAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].title == "Aprovada viola criterios do Juiz"
    failed = {item["metric"] for item in findings[0].metadata["failed"]}
    assert "profit_factor" in failed
    assert "max_drawdown_pct" in failed


def test_strategy_validator_accepts_robust_approved_strategy(tmp_path):
    catalog_path = tmp_path / "strategy_catalog.json"
    active_path = tmp_path / "active_strategies.json"
    _write_json(
        catalog_path,
        {
            "entries": {
                "strong-id": {
                    "id": "strong-id",
                    "template": "Trend",
                    "status": "approved",
                    "summary": {
                        "total_trades": 42,
                        "profit_factor": 1.45,
                        "max_drawdown_pct": 14.0,
                        "max_loss_streak": 2,
                        "consistency_score": 72.0,
                        "windows_passed": 4,
                        "windows_total": 5,
                    },
                }
            }
        },
    )
    _write_json(active_path, {"strategies": [{"id": "strong-id", "template": "Trend"}]})

    findings = StrategyValidatorAgent(catalog_path=catalog_path, active_path=active_path).run()

    assert findings[0].title == "Estrategias aprovadas passam na auditoria"


def test_portfolio_agent_flags_high_concentration(tmp_path):
    state_path = tmp_path / "paper" / "state.json"
    _write_json(
        state_path,
        {
            "BTCUSDT": {
                "strategy": "Test",
                "balance": 800.0,
                "initial_balance": 800.0,
                "total_trades": 5,
                "wins": 4,
                "losses": 1,
                "position": None,
                "risk": {},
            },
            "ETHUSDT": {
                "strategy": "Test",
                "balance": 200.0,
                "initial_balance": 200.0,
                "total_trades": 5,
                "wins": 3,
                "losses": 2,
                "position": None,
                "risk": {},
            },
        },
    )

    findings = PortfolioAgent(state_path=state_path, max_asset_weight_pct=60.0).run()

    assert findings[0].title == "Concentracao alta em ativo"
    assert findings[0].asset == "BTCUSDT"


def test_portfolio_agent_flags_weak_asset_with_high_weight(tmp_path):
    state_path = tmp_path / "paper" / "state.json"
    _write_json(
        state_path,
        {
            "SOLUSDT": {
                "strategy": "Test",
                "balance": 600.0,
                "initial_balance": 700.0,
                "total_trades": 5,
                "wins": 1,
                "losses": 4,
                "position": None,
                "risk": {},
            },
            "ADAUSDT": {
                "strategy": "Test",
                "balance": 400.0,
                "initial_balance": 400.0,
                "total_trades": 5,
                "wins": 3,
                "losses": 2,
                "position": None,
                "risk": {},
            },
        },
    )

    findings = PortfolioAgent(
        state_path=state_path,
        max_asset_weight_pct=90.0,
        weak_asset_weight_pct=25.0,
    ).run()

    assert findings[0].title == "Ativo fraco com peso alto"
    assert findings[0].asset == "SOLUSDT"


def test_policy_agent_writes_asset_kill_for_confirmed_critical(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_MIN_STREAK", "1")
    findings = [
        AgentFinding(
            agent="risk_agent",
            severity=AgentSeverity.CRITICAL,
            title="Drawdown acima do limite",
            message="XRP ruim",
            asset="XRPUSDT",
        )
    ]

    decisions = PolicyAgent(
        kill_switch_path=tmp_path / "KILL_SWITCH",
        kill_switch_assets_dir=tmp_path / "kill_switch",
        decisions_path=tmp_path / "policy_decisions.json",
        streaks_path=tmp_path / "policy_streaks.json",
    ).run_with_context(findings)

    assert decisions[0].title == "KILL por asset: XRPUSDT"
    assert decisions[0].metadata["kind"] == "asset"
    kill_file = tmp_path / "kill_switch" / "XRPUSDT.json"
    assert kill_file.exists()
    data = json.loads(kill_file.read_text(encoding="utf-8"))
    assert data["mode"] == "graceful"


def test_policy_agent_waits_for_min_streak_before_asset_kill(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_MIN_STREAK", "2")
    findings = [
        AgentFinding(
            agent="risk_agent",
            severity=AgentSeverity.CRITICAL,
            title="Drawdown acima do limite",
            message="ETH ruim",
            asset="ETHUSDT",
        )
    ]

    policy = PolicyAgent(
        kill_switch_path=tmp_path / "KILL_SWITCH",
        kill_switch_assets_dir=tmp_path / "kill_switch",
        decisions_path=tmp_path / "policy_decisions.json",
        streaks_path=tmp_path / "policy_streaks.json",
    )

    assert policy.run_with_context(findings) == []
    assert not (tmp_path / "kill_switch" / "ETHUSDT.json").exists()

    decisions = policy.run_with_context(findings)
    assert decisions[0].title == "KILL por asset: ETHUSDT"
    assert (tmp_path / "kill_switch" / "ETHUSDT.json").exists()


def test_policy_agent_reports_continue_for_clean_assets(tmp_path):
    decisions_path = tmp_path / "policy_decisions.json"
    decisions = PolicyAgent(
        kill_switch_path=tmp_path / "KILL_SWITCH",
        kill_switch_assets_dir=tmp_path / "kill_switch",
        decisions_path=decisions_path,
        streaks_path=tmp_path / "policy_streaks.json",
    ).run_with_context([])

    assert decisions == []
    assert decisions_path.exists()
    data = json.loads(decisions_path.read_text(encoding="utf-8"))
    assert data["criticals_seen"] == 0


def test_policy_agent_deduplicates_global_drivers(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_MIN_STREAK", "1")
    duplicate = AgentFinding(
        agent="health_agent",
        severity=AgentSeverity.CRITICAL,
        title="Log gigante",
        message="log",
        metadata={"path": "src/logs/trading_bot.log"},
    )

    decisions = PolicyAgent(
        kill_switch_path=tmp_path / "KILL_SWITCH",
        kill_switch_assets_dir=tmp_path / "kill_switch",
        decisions_path=tmp_path / "policy_decisions.json",
        streaks_path=tmp_path / "policy_streaks.json",
    ).run_with_context([duplicate, duplicate])
    global_pause = [finding for finding in decisions if finding.title == "KILL SWITCH GLOBAL acionado"][0]

    assert len(global_pause.metadata["drivers"]) == 1


def test_execution_agent_flags_old_position(tmp_path):
    state_path = tmp_path / "state.json"
    open_time = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = open_time + timedelta(hours=80)
    _write_json(
        state_path,
        {
            "BTCUSDT": {
                "balance": 100.0,
                "initial_balance": 100.0,
                "total_trades": 1,
                "wins": 0,
                "losses": 1,
                "position": {
                    "entry_price": 50000.0,
                    "qty": 0.001,
                    "time": open_time.isoformat().replace("+00:00", "Z"),
                },
                "risk": {},
            }
        },
    )

    findings = ExecutionAgent(state_path=state_path, now=now).run()
    titles = {f.title for f in findings}
    assert "Posicao aberta ha tempo demais" in titles


def test_execution_agent_flags_stale_runtime_log(tmp_path):
    runtime = tmp_path / "bot_runtime.log"
    runtime.write_text("ok", encoding="utf-8")
    import os, time
    old = time.time() - 60 * 60
    os.utime(runtime, (old, old))

    findings = ExecutionAgent(
        state_path=tmp_path / "state.json",
        thread_log_path=tmp_path / "thread_debug.log",
        runtime_log_path=runtime,
    ).run()
    titles = {f.title for f in findings}
    assert "Loop possivelmente travado" in titles


def test_execution_agent_flags_duplicate_runtime_processes(tmp_path):
    snapshot = [
        {"pid": 101, "command": "python -u -m src.main"},
        {"pid": 102, "command": "python -u -m src.main"},
        {"pid": 201, "command": "python -u -m src.research.aether.auto_deploy"},
        {"pid": 202, "command": "python -u -m src.research.aether.auto_deploy"},
    ]

    findings = ExecutionAgent(
        state_path=tmp_path / "state.json",
        thread_log_path=tmp_path / "thread_debug.log",
        runtime_log_path=tmp_path / "bot_runtime.log",
        process_snapshot=snapshot,
    ).run()

    duplicate = [f for f in findings if f.title == "Processos duplicados no runtime"][0]
    assert duplicate.severity == AgentSeverity.CRITICAL
    assert duplicate.metadata["duplicates"]["src.main"] == [101, 102]
    assert duplicate.metadata["duplicates"]["src.research.aether.auto_deploy"] == [201, 202]


def test_execution_agent_ignores_python_launcher_child_pair(tmp_path):
    snapshot = [
        {"pid": 101, "parent_pid": 1, "command": "python -u -m src.main"},
        {"pid": 102, "parent_pid": 101, "command": "python -u -m src.main"},
    ]

    findings = ExecutionAgent(
        state_path=tmp_path / "state.json",
        thread_log_path=tmp_path / "thread_debug.log",
        runtime_log_path=tmp_path / "bot_runtime.log",
        process_snapshot=snapshot,
    ).run()

    assert all(f.title != "Processos duplicados no runtime" for f in findings)


def test_data_quality_agent_flags_negative_balance(tmp_path):
    state_path = tmp_path / "state.json"
    _write_json(
        state_path,
        {
            "ETHUSDT": {
                "balance": -10.0,
                "initial_balance": 100.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "position": None,
                "risk": {},
            }
        },
    )

    findings = DataQualityAgent(state_path=state_path).run()
    titles = {f.title for f in findings}
    assert "Balance negativo" in titles


def test_data_quality_agent_flags_position_missing_fields(tmp_path):
    state_path = tmp_path / "state.json"
    _write_json(
        state_path,
        {
            "ADAUSDT": {
                "balance": 100.0,
                "initial_balance": 100.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "position": {"entry_price": 1.0},
                "risk": {},
            }
        },
    )

    findings = DataQualityAgent(state_path=state_path).run()
    titles = {f.title for f in findings}
    assert "Posicao sem campos obrigatorios" in titles


def test_data_quality_agent_clean_when_state_ok(tmp_path):
    state_path = tmp_path / "state.json"
    _write_json(
        state_path,
        {
            "BTCUSDT": {
                "balance": 100.0,
                "initial_balance": 100.0,
                "total_trades": 4,
                "wins": 2,
                "losses": 2,
                "position": None,
                "risk": {"consecutive_losses": 1},
            }
        },
    )

    findings = DataQualityAgent(state_path=state_path).run()
    assert findings[0].title == "Inspecao limpa"


def test_drift_agent_flags_pf_drift(tmp_path):
    state_path = tmp_path / "state.json"
    catalog_path = tmp_path / "catalog.json"
    active_path = tmp_path / "active.json"
    _write_json(
        state_path,
        {
            "BTCUSDT": {
                "strategy": "ChampStrategy",
                "balance": 90.0,
                "initial_balance": 100.0,
                "total_trades": 20,
                "wins": 5,
                "losses": 15,
                "position": None,
                "risk": {},
            }
        },
    )
    _write_json(
        catalog_path,
        {
            "entries": [
                {
                    "id": "ChampStrategy",
                    "name": "ChampStrategy",
                    "summary": {
                        "full": {
                            "profit_factor": 2.0,
                            "winrate_pct": 60.0,
                            "max_drawdown_pct": 8.0,
                            "total_trades": 100,
                        }
                    },
                }
            ]
        },
    )
    _write_json(active_path, {"strategies": []})

    findings = DriftAgent(
        state_path=state_path,
        catalog_path=catalog_path,
        active_path=active_path,
        min_trades=10,
    ).run()
    titles = {f.title for f in findings}
    assert any("drift" in t.lower() or "Drift" in t for t in titles)


def test_market_regime_agent_flags_regime_mismatch(tmp_path):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    closes = [100 + i * 1.5 for i in range(60)]
    _write_json(candles_dir / "BTCUSDT.json", {"candles": [{"close": c} for c in closes]})

    active_path = tmp_path / "active.json"
    _write_json(
        active_path,
        {"strategies": [{"asset": "BTCUSDT", "validated_regime": "lateral"}]},
    )

    findings = MarketRegimeAgent(active_path=active_path, candles_dir=candles_dir).run()
    titles = {f.title for f in findings}
    assert "Regime atual diverge do validado" in titles


def test_market_regime_agent_no_candles_dir(tmp_path):
    findings = MarketRegimeAgent(
        active_path=tmp_path / "active.json",
        candles_dir=tmp_path / "missing",
    ).run()
    titles = {f.title for f in findings}
    assert "Sem cache de candles" in titles


def _fake_caller_factory(hypotheses):
    def caller(*args, **kwargs):
        return {
            "ok": True,
            "provider": "openai",
            "model": "test-model",
            "text": json.dumps({"hypotheses": hypotheses}),
        }
    return caller


def test_aether_researcher_skips_when_openai_not_configured(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    monkeypatch.setattr(
        "src.research.aether.llm_runtime.llm_status",
        lambda: {
            "providers": {"openai": {"configured": False}},
            "roles": {"hypothesis_generator": {"provider": "openai", "configured": False}},
        },
    )

    findings = AetherResearcherAgent(
        catalog_path=catalog,
        templates=("RSI_Reversal",),
    ).run()
    titles = {f.title for f in findings}
    assert "LLM do Aether nao configurado" in titles
    assert not catalog.exists()


def test_aether_researcher_uses_configured_role_fallback(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    monkeypatch.setattr(
        "src.research.aether.llm_runtime.llm_status",
        lambda: {
            "providers": {"openai": {"configured": True}},
            "roles": {
                "hypothesis_generator": {
                    "provider": "openai",
                    "configured": True,
                    "fallbacks": [],
                }
            },
        },
    )
    monkeypatch.delenv("AETHER_RESEARCHER_WRITE_ENABLED", raising=False)

    caller = _fake_caller_factory([
        {
            "slug": "rsi-fallback",
            "name": "RSI fallback",
            "template": "RSI_Reversal",
            "params": {"period": 10},
            "risk": {"take_profit_pct": 2.0, "stop_loss_pct": 1.5},
            "hypothesis": "Tese",
            "causal_mechanism": "Mecanismo",
            "invalidation": "Invalida",
            "base_score": 0.55,
        }
    ])

    findings = AetherResearcherAgent(
        catalog_path=catalog,
        templates=("RSI_Reversal",),
        caller=caller,
    ).run()

    assert any("hipotese(s) Aether disponivel" in f.title for f in findings)
    assert not catalog.exists()


def test_llm_runtime_falls_back_to_openai_when_claude_fails(monkeypatch):
    from src.research.aether import llm_runtime

    llm_runtime._CB_STATE.clear()
    monkeypatch.setenv("AETHER_PRIMARY_LLM_PROVIDER", "claude")
    monkeypatch.setenv("AETHER_LLM_FALLBACK_PROVIDERS", "openai")
    monkeypatch.setenv("AETHER_CLAUDE_TRANSPORT", "api")
    monkeypatch.setenv("AETHER_OPENAI_TRANSPORT", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("AETHER_LLM_FALLBACK_ENABLED", "1")

    def fail_claude(**kwargs):
        raise RuntimeError("usage limit reached")

    def ok_codex(**kwargs):
        return {"text": '{"hypotheses": []}', "metadata": {"source": "test"}}

    monkeypatch.setattr(llm_runtime, "call_claude_result", fail_claude)
    monkeypatch.setattr(llm_runtime, "call_openai_codex_result", ok_codex)

    response = llm_runtime.call_llm_agent(
        "hypothesis_generator",
        system="system",
        prompt="prompt",
    )

    assert response["ok"] is True
    assert response["provider"] == "openai"
    assert response["metadata"]["fallback"] is True
    assert "usage limit reached" in response["metadata"]["previous_errors"][0]


def test_openai_api_result_records_usage_cost(monkeypatch):
    from src.research.aether import llm_runtime

    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")

    def fake_post_json(url, headers, payload, timeout):
        captured["payload"] = payload
        return {
            "id": "resp_test",
            "output_text": '{"hypotheses": []}',
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "input_tokens_details": {"cached_tokens": 400},
            },
        }

    monkeypatch.setattr(llm_runtime, "_post_json", fake_post_json)
    provider = llm_runtime.LLMProvider(
        name="openai",
        label="OpenAI",
        model="gpt-5.4-mini",
        effort=None,
        key_env="OPENAI_API_KEY",
        configured=True,
        transport="api",
    )

    result = llm_runtime.call_openai_codex_result(
        system="system",
        prompt="prompt",
        max_tokens=50,
        provider=provider,
    )

    assert captured["payload"]["model"] == "gpt-5.4-mini"
    assert result["text"] == '{"hypotheses": []}'
    assert result["metadata"]["usage"]["input_tokens"] == 1000
    assert result["metadata"]["cost_usd"] == 0.00138


def test_llm_runtime_auth_error_opens_long_circuit(monkeypatch):
    from src.research.aether import llm_runtime

    llm_runtime._CB_STATE.clear()
    calls = {"n": 0}
    monkeypatch.setenv("AETHER_PRIMARY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AETHER_OPENAI_TRANSPORT", "api")
    monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
    monkeypatch.setenv("AETHER_LLM_AUTH_COOLDOWN", "600")
    monkeypatch.delenv("AETHER_LLM_FALLBACK_PROVIDERS", raising=False)

    def fail_openai(**kwargs):
        calls["n"] += 1
        raise RuntimeError("401 Unauthorized: Incorrect API key provided")

    monkeypatch.setattr(llm_runtime, "call_openai_codex_result", fail_openai)

    first = llm_runtime.call_llm_agent("hypothesis_generator", system="s", prompt="p")
    second = llm_runtime.call_llm_agent("hypothesis_generator", system="s", prompt="p")

    assert first["ok"] is False
    assert second["ok"] is False
    assert calls["n"] == 1
    assert "circuit breaker aberto" in second["error"]


def test_claude_code_does_not_inherit_external_api_keys(monkeypatch):
    from types import SimpleNamespace

    from src.research.aether import llm_runtime

    captured = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-anthropic")
    monkeypatch.setenv("AETHER_ANTHROPIC_API_KEY", "bad-aether-anthropic")
    monkeypatch.setattr(llm_runtime.shutil, "which", lambda name: "claude")

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": '{"hypotheses": []}'}),
            stderr="",
        )

    monkeypatch.setattr(llm_runtime.subprocess, "run", fake_run)

    result = llm_runtime.call_claude_code_result(
        system="system",
        prompt="prompt",
        max_tokens=50,
    )

    assert result["text"] == '{"hypotheses": []}'
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "AETHER_ANTHROPIC_API_KEY" not in captured["env"]


def test_codex_cli_does_not_inherit_openai_api_keys(monkeypatch):
    from types import SimpleNamespace

    from src.research.aether import llm_runtime

    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "bad-openai")
    monkeypatch.setenv("AETHER_OPENAI_API_KEY", "bad-aether-openai")
    monkeypatch.setenv("AETHER_CODEX_TRANSPORT", "codex_cli")
    monkeypatch.setattr(llm_runtime.shutil, "which", lambda name: "codex")

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        output_path = args[0][args[0].index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write('{"hypotheses": []}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(llm_runtime.subprocess, "run", fake_run)

    result = llm_runtime.call_codex_cli_result(
        system="system",
        prompt="prompt",
        max_tokens=50,
    )

    assert result["text"] == '{"hypotheses": []}'
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "AETHER_OPENAI_API_KEY" not in captured["env"]


def test_codex_cli_uses_explicit_path(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.research.aether import llm_runtime

    fake_cli = tmp_path / "codex.exe"
    fake_cli.write_text("", encoding="utf-8")
    captured = {}
    monkeypatch.setenv("AETHER_CODEX_CLI_PATH", str(fake_cli))
    monkeypatch.setenv("AETHER_OPENAI_TRANSPORT", "codex_cli")
    monkeypatch.setattr(llm_runtime.shutil, "which", lambda name: None)
    monkeypatch.setattr(llm_runtime, "_cli_usable", lambda path: path == str(fake_cli))

    def fake_run(*args, **kwargs):
        captured["args"] = args[0]
        output_path = args[0][args[0].index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("OK")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(llm_runtime.subprocess, "run", fake_run)

    provider = llm_runtime._provider_for_name("openai")
    result = llm_runtime.call_codex_cli_result(system="system", prompt="prompt", provider=provider)

    assert provider.configured is True
    assert provider.cli_path == str(fake_cli)
    assert captured["args"][0] == str(fake_cli)
    assert result["text"] == "OK"


def test_aether_researcher_returns_preview_without_writing(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    monkeypatch.setattr(
        "src.research.aether.llm_runtime.llm_status",
        lambda: {"providers": {"openai": {"configured": True}}},
    )
    monkeypatch.delenv("AETHER_RESEARCHER_WRITE_ENABLED", raising=False)

    caller = _fake_caller_factory([
        {
            "slug": "rsi-aggressive",
            "name": "RSI agressivo",
            "template": "RSI_Reversal",
            "params": {"period": 14, "oversold": 25, "overbought": 75},
            "risk": {"take_profit_pct": 2.0, "stop_loss_pct": 1.5},
            "hypothesis": "Tese",
            "causal_mechanism": "Mecanismo",
            "invalidation": "Invalida quando",
            "base_score": 0.6,
        }
    ])

    findings = AetherResearcherAgent(
        catalog_path=catalog,
        templates=("RSI_Reversal",),
        caller=caller,
    ).run()

    assert any("hipotese(s) Aether disponivel" in f.title for f in findings)
    assert not catalog.exists()


def test_aether_researcher_writes_when_flag_enabled(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    monkeypatch.setattr(
        "src.research.aether.llm_runtime.llm_status",
        lambda: {"providers": {"openai": {"configured": True}}},
    )
    monkeypatch.setenv("AETHER_RESEARCHER_WRITE_ENABLED", "1")

    caller = _fake_caller_factory([
        {
            "slug": "rsi-aggressive",
            "name": "RSI agressivo",
            "template": "RSI_Reversal",
            "params": {"period": 14, "oversold": 25, "overbought": 75},
            "risk": {"take_profit_pct": 2.0, "stop_loss_pct": 1.5},
            "hypothesis": "Tese",
            "causal_mechanism": "Mecanismo",
            "invalidation": "Invalida",
            "base_score": 0.6,
        }
    ])

    findings = AetherResearcherAgent(
        catalog_path=catalog,
        templates=("RSI_Reversal",),
        caller=caller,
    ).run()

    assert any("candidata(s) Aether adicionada" in f.title for f in findings)
    assert catalog.exists()
    data = json.loads(catalog.read_text(encoding="utf-8"))
    entries = data["entries"]
    assert len(entries) == 1
    entry = next(iter(entries.values()))
    assert entry["status"] == "pending"
    assert entry["origin"] == "aether_llm"
    assert entry["template"] == "RSI_Reversal"


def test_aether_researcher_filters_unknown_template(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    monkeypatch.setattr(
        "src.research.aether.llm_runtime.llm_status",
        lambda: {"providers": {"openai": {"configured": True}}},
    )
    monkeypatch.setenv("AETHER_RESEARCHER_WRITE_ENABLED", "1")

    caller = _fake_caller_factory([
        {
            "slug": "wat",
            "name": "Template inexistente",
            "template": "NotARealTemplate",
            "params": {},
            "risk": {},
            "hypothesis": "x",
            "causal_mechanism": "x",
            "invalidation": "x",
            "base_score": 0.5,
        }
    ])

    findings = AetherResearcherAgent(
        catalog_path=catalog,
        templates=("RSI_Reversal",),
        caller=caller,
    ).run()
    titles = {f.title for f in findings}
    assert "Aether nao trouxe hipoteses validas" in titles
    assert not catalog.exists()
