from __future__ import annotations

LEGACY_CAMPAIGN_ID = "legacy_pre_aether"
CURRENT_CAMPAIGN_ID = "aether_current"
ACTIVE_CAMPAIGN_SETTING = "active_campaign_id"

DEFAULT_CAMPAIGNS = [
    {
        "id": LEGACY_CAMPAIGN_ID,
        "label": "Antes / legado",
        "kind": "legacy",
        "description": "Historico importado do projeto anterior, antes do Aether com campanhas.",
        "status": "archived",
    },
    {
        "id": CURRENT_CAMPAIGN_ID,
        "label": "Agora / Aether atual",
        "kind": "current",
        "description": "Campanha atual criada a partir do Aether.",
        "status": "active",
    },
]

LEGACY_SOURCE_TYPES = {"approved", "staging", "diag", "legacy_root"}
