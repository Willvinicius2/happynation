import inspect
import os


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


class StrategyRunner:

    @staticmethod
    def execute(
        self, main_strategy, fallback_strategy, stock_data, main_strategy_args=None, fallback_strategy_args=None, verbose=True,
        tf_data=None,
    ):
        """
        Executa a estrategia principal e, se necessario, a estrategia de fallback.

        Antes da estrategia fixa, consulta `src/logs/research/active_strategies.json`.
        O painel Aether grava ali as estrategias validadas para o bot testar em paper.
        """
        from src.research.active import active_manifest_enabled, get_active_specs

        main_strategy_args = dict(main_strategy_args or {})
        sig_main = inspect.signature(main_strategy)
        active_specs = []
        aether_only = _env_truthy("AETHER_STRATEGIES_ONLY", True)

        if active_manifest_enabled():
            asset = getattr(self, "operation_code", None)
            active_specs = get_active_specs(asset=asset)
            any_active_specs = get_active_specs() if aether_only else []
            if aether_only and any_active_specs and not active_specs:
                if getattr(self, "actual_trade_position", False) and _env_truthy("AETHER_EXIT_NO_ACTIVE_STRATEGY", False):
                    print(f"Aether-only: sem estrategia validada para {asset}; saindo da posicao.")
                    return False
                print(f"Aether-only: sem estrategia validada para {asset}; aguardando.")
                return True if getattr(self, "actual_trade_position", False) else None

        if active_specs and "active_strategy_specs" in sig_main.parameters:
            main_strategy_args["active_strategy_specs"] = active_specs
            if "aether_only" in sig_main.parameters:
                main_strategy_args["aether_only"] = aether_only
            if tf_data and "tf_data" in sig_main.parameters:
                main_strategy_args["tf_data"] = tf_data
        elif active_specs:
            for spec in active_specs:
                final_decision = spec.function(stock_data=stock_data, verbose=verbose, **spec.params)
                if final_decision is False and not getattr(self, "actual_trade_position", False):
                    print(f"Estrategia ativa sem acao compradora: {spec.name} -> Aguardar")
                    continue
                if final_decision is not None:
                    print(f"Estrategia ativa selecionada: {spec.name} -> {final_decision}")
                    return final_decision
            if aether_only:
                if getattr(self, "actual_trade_position", False):
                    print("Aether-only: estrategias ativas inconclusivas; mantendo posicao.")
                    return True
                print("Aether-only: estrategias ativas sem compra; consultando estrategia principal.")

        main_strategy_args["stock_data"] = stock_data
        main_strategy_args["verbose"] = verbose
        if tf_data and "tf_data" in sig_main.parameters and "tf_data" not in main_strategy_args:
            main_strategy_args["tf_data"] = tf_data

        if "self" in sig_main.parameters:
            main_strategy_args["self"] = self

        final_decision = main_strategy(**main_strategy_args)

        if final_decision is None and self.fallback_activated:
            print("Estrategia principal inconclusiva\nExecutando estrategia de fallback...")

            fallback_strategy_args = dict(fallback_strategy_args or {})
            fallback_strategy_args["stock_data"] = stock_data
            fallback_strategy_args["verbose"] = verbose

            final_decision = fallback_strategy(**fallback_strategy_args)

        return final_decision
