import pandas as pd


def getDevStrategy(stock_data: pd.DataFrame, decision, verbose=True):
    if verbose:
        print("\n[DEBUG] Estratégia Dev: ", decision)

    if decision:
        if verbose:
            print("[DEBUG] Estratégia Retorna: True (comprar)")
        return True
    if not decision:
        if verbose:
            print("[DEBUG] Estratégia Retorna: False (vender)")
        return False
    else:
        if verbose:
            print("[DEBUG] Estratégia Retorna: None (não fazer nada)")
        return None
