from pathlib import Path


REMOVED_SETTING_REFERENCES = (
    'settings.market_data_mode',
    'settings.poll_interval_seconds',
    'settings.market_data_queue_capacity',
    'settings.ws_symbol_silence_seconds',
    'settings.ws_position_silence_seconds',
    'settings.ws_global_silence_seconds',
    'settings.rest_control_interval_seconds',
    'settings.rest_fallback_cooldown_seconds',
    'settings.position_fallback_interval_seconds',
    'settings.candle_clock_grace_seconds',
    'settings.position_reconciliation_grace_seconds',
    'settings.unknown_order_lookup_interval_seconds',
)


def test_application_no_longer_reads_removed_runtime_policy_from_settings():
    source_by_path = {
        path: path.read_text(encoding='utf-8')
        for path in Path('app').rglob('*.py')
    }

    for removed in REMOVED_SETTING_REFERENCES:
        offenders = [
            str(path)
            for path, source in source_by_path.items()
            if removed in source
        ]
        assert offenders == [], f'{removed} remains in {offenders}'

    # Runtime safety policy stays code-versioned in the V3 runtime instead of
    # being reintroduced as mutable deployment settings. Do not couple this
    # contract test to quote style or source formatting in app/main.py.
    v3_runtime_source = source_by_path[Path('app/v3/runtime.py')]
    assert 'from app.runtime.runtime_policy import (' in v3_runtime_source
