from profit_engine import cli


class NoopPipeline:
    def __init__(self, *a, **kw):
        pass

    def run_forever(self, cycles=None):
        pass


def test_ingest_starts_maker_in_same_process(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_maker_thread", lambda db, series, interval: started.append((db, series, interval)))
    monkeypatch.setattr(cli, "Pipeline", NoopPipeline)
    rc = cli.main(["--db", str(tmp_path / "r.db"), "ingest", "--kalshi-series", "KXHIGHNY",
                   "--maker-series", "KXHIGHNY", "--maker-db", str(tmp_path / "m.db"), "--cycles", "1"])
    assert rc == 0 and started == [(str(tmp_path / "m.db"), ["KXHIGHNY"], 10.0)]


def test_maker_needs_its_own_database(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "start_maker_thread", lambda *a: (_ for _ in ()).throw(AssertionError("must not start")))
    db = str(tmp_path / "r.db")
    assert cli.main(["--db", db, "ingest", "--kalshi-series", "KXHIGHNY", "--maker-series", "KXHIGHNY",
                     "--maker-db", db, "--cycles", "1"]) == 2


def test_ingest_without_maker_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "start_maker_thread", lambda *a: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(cli, "Pipeline", NoopPipeline)
    assert cli.main(["--db", str(tmp_path / "r.db"), "ingest", "--kalshi-series", "KXHIGHNY", "--cycles", "1"]) == 0
