from __future__ import annotations


def test_run_sweep_continues_when_checkpoint_write_fails(tmp_path, monkeypatch):
    from src.research.aether import compute

    def locked_checkpoint(*_args, **_kwargs):
        raise PermissionError("checkpoint locked")

    progress: list[dict] = []
    monkeypatch.setattr(compute, "_write_checkpoint", locked_checkpoint)

    result = compute.run_sweep(
        probes=[],
        workers=1,
        sweep_id="test-sweep",
        checkpoint_path=tmp_path / "test-sweep.json",
        progress_callback=progress.append,
    )

    assert result["status"] == "completed"
    assert result["completed"] == 0
    assert progress
    assert any("checkpoint_error" in item for item in progress)
