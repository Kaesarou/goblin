"""Run Goblin V3 Point-M replay against an externally supplied candle corpus.

This script intentionally does not download data. The caller supplies the reconstructed
M1 corpus so repository code cannot silently mutate the scientific sample.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import numpy as np

from app.v3.config import InventoryRiskConfig, RecoverabilityConfig, rr5_research_config
from app.v3.economics import BrokerCostSchedule, EconomicsModel
from app.v3.planner import InventoryPlanner
from app.v3.recoverability import RecoverabilityScorer
from app.v3.replay import InventoryReplayEngine, M1FeatureFrameBuilder
from app.v3.risk import InventoryRiskPolicy
from app.v3.point_m import (
    MAX7_GOLDEN,
    POINT_M_END,
    POINT_M_ROWS,
    POINT_M_START,
    POINT_M_SYMBOLS,
    RR5_GOLDEN,
    assert_point_m,
)


def _load(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    return pd.read_csv(path)


def _engine(config):
    scorer = RecoverabilityScorer.from_default_artifact()
    planner = InventoryPlanner(
        config=config,
        recoverability_scorer=scorer,
        risk_policy=InventoryRiskPolicy(config.risk),
        economics_model=EconomicsModel(config.economics, BrokerCostSchedule()),
    )
    return InventoryReplayEngine(
        planner=planner,
        config=config,
        fee_pct_per_fill=0.00225,
        forager_mode="no_volume",
        research_proxy_mode=True,
        ending_force_close=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candles", type=Path)
    parser.add_argument("--already-featured", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assert-golden", action="store_true")
    args = parser.parse_args()

    frame = _load(args.candles)
    if not args.already_featured:
        frame = M1FeatureFrameBuilder().build(frame)

    rr5 = rr5_research_config()
    rr5 = replace(rr5, recoverability=RecoverabilityConfig(enabled=False))

    max7 = replace(
        rr5,
        risk=InventoryRiskConfig(
            max_entry_fills=7,
            max_symbol_exposure_pct=(1.5 / 7) * 1.37,
            max_portfolio_exposure_pct=1.5,
            max_inventories=7,
            catastrophic_portfolio_drawdown_pct=0.10,
        ),
    )

    max7_metrics, _, _ = _engine(max7).run(frame)
    rr5_metrics, _, _ = _engine(rr5).run(frame)
    result = {
        "rows": int(len(frame)),
        "symbols": int(frame.symbol.nunique()),
        "max7": asdict(max7_metrics),
        "rr5": asdict(rr5_metrics),
    }
    if args.assert_golden:
        opened_at = pd.to_datetime(frame["opened_at"], utc=True)
        actual_meta = {
            "rows": int(len(frame)),
            "symbols": int(frame.symbol.nunique()),
            "start": opened_at.min().isoformat(),
            "end": opened_at.max().isoformat(),
        }
        expected_meta = {
            "rows": POINT_M_ROWS,
            "symbols": POINT_M_SYMBOLS,
            "start": POINT_M_START,
            "end": POINT_M_END,
        }
        if actual_meta != expected_meta:
            raise AssertionError(
                f"Point-M corpus mismatch: actual={actual_meta}, expected={expected_meta}"
            )
        assert_point_m(max7_metrics, MAX7_GOLDEN)
        assert_point_m(rr5_metrics, RR5_GOLDEN)
        result["golden_assertions"] = "passed"

    def _json_default(value):
        if isinstance(value, np.generic):
            return value.item()
        raise TypeError(f"Unsupported JSON value: {type(value)!r}")

    text = json.dumps(result, indent=2, default=_json_default)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
