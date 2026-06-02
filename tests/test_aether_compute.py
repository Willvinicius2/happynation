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


def test_run_sweep_keeps_batch_when_process_pool_future_fails(tmp_path, monkeypatch):
    from src.research.aether import compute

    class FailedFuture:
        def result(self):
            raise RuntimeError("process pool died")

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, *_args, **_kwargs):
            return FailedFuture()

    monkeypatch.setattr(compute, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(compute, "as_completed", lambda futures: list(futures))
    monkeypatch.setenv("AETHER_PREWARM_DATASETS", "0")

    result = compute.run_sweep(
        probes=[{"probe_id": "p1", "dataset_id": "binance:BTCUSDT:1h"}],
        workers=2,
        sweep_id="pool-failure",
        checkpoint_path=tmp_path / "pool-failure.json",
    )

    assert result["status"] == "completed"
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["probe_id"] == "p1"
    assert "RuntimeError: process pool died" in result["results"][0]["error"]
