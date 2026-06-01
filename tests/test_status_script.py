from __future__ import annotations

from pathlib import Path

import scripts.status as status


def _write_auto_deploy_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "2026-06-01 15:01:38,893 INFO deploy: candidates=184 struct_dedupe=128 func_dropped=17 after_dedupe=29 deployed=0 skipped_unchanged=29 removed_stale=0 blocked=15",
                "2026-06-01 15:01:38,893 INFO deploy detail: gate=score_below_min:211,mdd_above_max:25 blocklist=heavy_validation_cut_or_revalidate:5,manifest_top_validation_trade_sample:3 caps=asset_cap:55,family_cap:24",
            ]
        ),
        encoding="utf-8",
    )


def test_auto_deploy_log_snapshot_parses_latest_summary_and_reasons(tmp_path):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)

    snapshot = status.latest_auto_deploy_snapshot(log_path)

    assert snapshot["summary"]["candidates"] == 184
    assert snapshot["summary"]["after_dedupe"] == 29
    assert snapshot["summary"]["blocked"] == 15
    assert snapshot["details"]["gate"] == {"score_below_min": 211, "mdd_above_max": 25}
    assert snapshot["details"]["blocklist"]["heavy_validation_cut_or_revalidate"] == 5
    assert snapshot["details"]["caps"] == {"asset_cap": 55, "family_cap": 24}


def test_auto_deploy_log_snapshot_does_not_attach_stale_detail(tmp_path):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            "\n2026-06-01 15:02:38,893 INFO deploy: candidates=200 struct_dedupe=130 "
            "func_dropped=20 after_dedupe=31 deployed=1 skipped_unchanged=30 removed_stale=0 blocked=0"
        )

    snapshot = status.latest_auto_deploy_snapshot(log_path)

    assert snapshot["summary"]["candidates"] == 200
    assert snapshot["details"] == {}


def test_show_auto_deploy_flow_prints_actionable_bottlenecks(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)
    monkeypatch.setattr(status, "AUTO_DEPLOY_LOG", log_path)

    status.show_auto_deploy_flow()

    out = capsys.readouterr().out
    assert "FUNIL AUTO-DEPLOY AETHER" in out
    assert "candidates=184 after_dedupe=29 deployed=0" in out
    assert "score_below_min: 211" in out
    assert "asset_cap: 55" in out
