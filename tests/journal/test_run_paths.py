from app.journal.run_paths import build_run_journal_paths, rotate_run_journals


def test_run_paths_are_compressed_and_isolated(tmp_path):
    paths = build_run_journal_paths(
        journal_path=str(tmp_path / 'data' / 'logs' / 'trades.jsonl'),
        run_id='run-123',
    )

    logs_root = tmp_path / 'data' / 'logs'
    assert paths.root == logs_root / 'runs' / 'run-123'
    assert paths.trades.name == 'trades.jsonl.gz'
    assert paths.market.name == 'market.jsonl.gz'
    assert paths.candles.name == 'candles.jsonl.gz'
    assert paths.errors.name == 'errors.jsonl.gz'
    assert paths.debug_decisions.name == 'debug_decisions.jsonl.gz'
    assert paths.research.name == 'research.jsonl.gz'
    assert paths.research_summary.name == 'research_summary.json'
    assert paths.etoro_payload_schema.name == 'etoro_payload_schema.json'
    for path in paths.__dict__.values():
        assert path == logs_root or logs_root in path.parents
    assert paths.root.exists()


def test_run_rotation_never_deletes_unarchived_runs(tmp_path):
    runs_root = tmp_path / 'runs'
    current = runs_root / 'run-current'
    current.mkdir(parents=True)
    old = runs_root / 'run-old'
    old.mkdir()
    recent = runs_root / 'run-recent'
    recent.mkdir()
    (old / 'candles.jsonl.gz').write_bytes(b'historical M1 candles')
    (recent / 'trades.jsonl.gz').write_bytes(b'historical trades')

    removed = rotate_run_journals(
        runs_root=runs_root,
        max_runs=2,
        current_run_id='run-current',
    )

    assert removed == ()
    assert current.exists()
    assert (old / 'candles.jsonl.gz').read_bytes() == b'historical M1 candles'
    assert (recent / 'trades.jsonl.gz').read_bytes() == b'historical trades'


def test_hundreds_of_crash_restarts_cannot_evict_last_successful_run(tmp_path):
    runs_root = tmp_path / 'runs'
    historical = runs_root / 'run-previous-session'
    historical.mkdir(parents=True)
    (historical / 'candles.jsonl.gz').write_bytes(b'unarchived research evidence')
    (historical / 'trades.jsonl.gz').write_bytes(b'unarchived fills')

    for index in range(200):
        current = f'run-crashed-{index:04d}'
        (runs_root / current).mkdir()
        assert rotate_run_journals(
            runs_root=runs_root,
            max_runs=1,
            current_run_id=current,
        ) == ()

    assert (historical / 'candles.jsonl.gz').read_bytes() == b'unarchived research evidence'
    assert (historical / 'trades.jsonl.gz').read_bytes() == b'unarchived fills'
    assert len(list(runs_root.iterdir())) == 201
