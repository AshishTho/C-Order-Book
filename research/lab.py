"""Chronological BBO signal evaluation with delayed, depth-capped crossing fills.

Prices and costs use the replay engine's integer price unit (``tick``); PnL is
ticks * lots. A tick is the stored price unit, not necessarily the venue's
minimum quotation increment. No clock time or annualization is inferred.
Run ``python -m research.lab --help`` from the repository root.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass, replace
import hashlib
import html
import io
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Iterable, Sequence


SCHEMA = ("seq", "bid_price", "ask_price", "bid_qty", "ask_qty")
SIGNALS = ("imbalance", "microprice", "ofi", "random", "flat")


@dataclass(frozen=True)
class Snapshot:
    seq: int
    bid_price: int
    ask_price: int
    bid_qty: int
    ask_qty: int

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0


@dataclass(frozen=True)
class Config:
    train_fraction: float = 0.6
    horizon: int = 10
    delay: int = 1
    threshold_quantile: float = 0.75
    fee_ticks: float = 0.1
    slippage_ticks: float = 0.0
    order_size: int = 1
    seed: int = 42

    def validate(self) -> None:
        if any(type(value) is not int for value in (self.horizon, self.delay, self.order_size, self.seed)):
            raise ValueError("horizon, delay, order_size and seed must be integers")
        if not 0.1 <= self.train_fraction <= 0.9:
            raise ValueError("train_fraction must be between 0.1 and 0.9")
        if self.horizon < 1 or self.delay < 1:
            raise ValueError("horizon and delay must be at least 1 observation")
        if not 0.0 <= self.threshold_quantile < 1.0:
            raise ValueError("threshold_quantile must be in [0, 1)")
        if any(not math.isfinite(x) or x < 0 for x in
               (self.fee_ticks, self.slippage_ticks)):
            raise ValueError("fees and slippage must be finite and nonnegative")
        if self.order_size < 1:
            raise ValueError("order_size must be at least 1 lot")


@dataclass(frozen=True)
class Calibration:
    threshold: float
    direction: int
    train_count: int
    feature_mean: float
    label_mean_ticks: float
    correlation: float | None


def read_snapshots(path: Path) -> list[Snapshot]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return _parse_snapshots(stream)


def read_snapshot_source(path: Path) -> tuple[list[Snapshot], str]:
    """Parse and fingerprint the same bytes, even if the path changes later."""
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    rows = _parse_snapshots(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    return rows, digest


def _parse_snapshots(stream: Iterable[str]) -> list[Snapshot]:
    result: list[Snapshot] = []
    reader = csv.DictReader(stream)
    if (reader.fieldnames is None or any(x not in reader.fieldnames for x in SCHEMA)
            or len(set(reader.fieldnames)) != len(reader.fieldnames)):
        raise ValueError("BBO CSV requires columns: " + ",".join(SCHEMA))
    for row in reader:
        if None in row:
            raise ValueError(f"line {reader.line_num}: too many CSV fields")
        try:
            observation = Snapshot(*(int(row[name]) for name in SCHEMA))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"line {reader.line_num}: all BBO fields must be integers") from exc
        if not 0 <= observation.seq < 2**64 or (result and observation.seq <= result[-1].seq):
            raise ValueError(f"line {reader.line_num}: seq must strictly increase")
        if not 0 < observation.bid_price < observation.ask_price < 2**63:
            raise ValueError(f"line {reader.line_num}: require positive int64 bid_price < ask_price")
        if not 0 < min(observation.bid_qty, observation.ask_qty) <= max(observation.bid_qty, observation.ask_qty) < 2**63:
            raise ValueError(f"line {reader.line_num}: displayed quantities must be positive int64")
        result.append(observation)
    if not result:
        raise ValueError("BBO CSV contains no observations")
    return result


def validate_snapshot_rows(rows: Sequence[Snapshot], *, allow_invalid_books: bool = False) -> None:
    """Validate in-memory rows before evaluation, without converting numeric values.

    The data-quality path may retain crossed/empty integer books so its policy
    can report and reject them. Types, ranges and sequence order remain strict.
    """
    previous = None
    for index, row in enumerate(rows):
        where = f"rows[{index}]"
        if not isinstance(row, Snapshot):
            raise ValueError(f"{where} must be a Snapshot instance")
        if (type(row.seq) is not int or not 0 <= row.seq < 2**64
                or (previous is not None and row.seq <= previous)):
            raise ValueError(f"{where}.seq must be a uint64 integer and strictly increase")
        previous = row.seq
        for field in ("bid_price", "ask_price", "bid_qty", "ask_qty"):
            value = getattr(row, field)
            if type(value) is not int or not -(2**63) <= value < 2**63:
                raise ValueError(f"{where}.{field} must be an int64 integer")
        if not allow_invalid_books:
            if not 0 < row.bid_price < row.ask_price:
                raise ValueError(f"{where}: require positive int64 bid_price < ask_price")
            if min(row.bid_qty, row.ask_qty) <= 0:
                raise ValueError(f"{where}: displayed quantities must be positive int64")


def feature_series(rows: Sequence[Snapshot], seed: int = 42) -> dict[str, list[float]]:
    """Each feature at index t uses only information available at t."""
    series: dict[str, list[float]] = {name: [] for name in SIGNALS}
    rng = random.Random(seed)
    for index, row in enumerate(rows):
        total = row.bid_qty + row.ask_qty
        imbalance = (row.bid_qty - row.ask_qty) / total
        # Algebraically spread * imbalance / 2: these are related signals,
        # and exactly redundant up to scale under a constant spread.
        displacement = (row.ask_price - row.bid_price) * imbalance / 2.0
        ofi = 0.0
        if index:
            previous = rows[index - 1]
            increment = ((row.bid_price >= previous.bid_price) * row.bid_qty
                         - (row.bid_price <= previous.bid_price) * previous.bid_qty
                         - (row.ask_price <= previous.ask_price) * row.ask_qty
                         + (row.ask_price >= previous.ask_price) * previous.ask_qty)
            ofi = increment / total
        series["imbalance"].append(imbalance)
        series["microprice"].append(displacement)
        series["ofi"].append(ofi)
        series["random"].append(rng.uniform(-1.0, 1.0))
        series["flat"].append(0.0)
    return series


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile from no values")
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def correlation(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) < 2 or len(x) != len(y):
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    cross = sum((a - mx) * (b - my) for a, b in zip(x, y))
    variance = sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)
    return cross / math.sqrt(variance) if variance > 0 else None


def split_indices(count: int, config: Config) -> tuple[int, int]:
    config.validate()
    split = int(count * config.train_fraction)
    train_stop = split - config.delay - config.horizon
    if train_stop < 20 or count - split - config.delay - config.horizon < 20:
        raise ValueError("need at least 20 eligible train and test decisions after embargo")
    return train_stop, split


def markout(rows: Sequence[Snapshot], index: int, config: Config) -> float:
    entry = rows[index + config.delay]
    end = rows[index + config.delay + config.horizon]
    # Subtract integer price sums before converting, preserving small returns
    # even when absolute prices exceed binary64's exact-integer range.
    return (end.bid_price + end.ask_price - entry.bid_price - entry.ask_price) / 2.0


def calibrate(values: Sequence[float], rows: Sequence[Snapshot], train_stop: int,
              config: Config, name: str) -> Calibration:
    x = list(values[:train_stop])
    y = [markout(rows, index, config) for index in range(train_stop)]
    corr = correlation(x, y)
    direction = 0 if corr is None or corr == 0.0 else (1 if corr > 0 else -1)
    if name == "random":
        direction = 1  # The seeded null never learns a favorable orientation.
    if name == "flat":
        direction = 0
    return Calibration(quantile([abs(value) for value in x], config.threshold_quantile),
                       direction, len(x), statistics.fmean(x), statistics.fmean(y), corr)


def decision(value: float, calibration: Calibration) -> int:
    if not calibration.direction or value == 0 or abs(value) < calibration.threshold:
        return 0
    return calibration.direction * (1 if value > 0 else -1)


def predictive_metrics(values: Sequence[float], rows: Sequence[Snapshot], start: int,
                       stop: int, calibration: Calibration, config: Config) -> dict:
    x, labels, active = [], [], []
    for index in range(start, stop):
        label = markout(rows, index, config)
        x.append(values[index])
        labels.append(label)
        side = decision(values[index], calibration)
        if side:
            active.append(side * label)
    nonzero = [value for value in active if value != 0]
    return {
        "decision_observations": len(x),
        "active_decisions": len(active),
        "feature_markout_correlation": correlation(x, labels),
        "mean_signed_markout_ticks": statistics.fmean(active) if active else None,
        "nonzero_markout_direction_accuracy":
            sum(value > 0 for value in nonzero) / len(nonzero) if nonzero else None,
        "zero_markouts": len(active) - len(nonzero),
        "inference_warning": "Overlapping event-time labels; no IID p-value or annualized Sharpe.",
    }


def simulate(rows: Sequence[Snapshot], values: Sequence[float], start: int,
             calibration: Calibration, config: Config) -> dict:
    """One inventory position, delayed entry, top-depth capped entry and exits.

    A decision at t can enter only at t+delay. An open position begins exiting
    horizon observations after its fill; insufficient top depth carries the
    remainder into subsequent observations. No new order is decided while a
    position or entry is pending. Entry cancellations/revisions are not modeled.
    """
    cash = 0  # Exact gross integer cash; floating costs are kept separately.
    inventory = 0
    pending: tuple[int, int, int] | None = None
    exit_due = -1
    open_trade: dict | None = None
    trades: list[dict] = []
    curve: list[dict] = []
    peak, max_drawdown = 0.0, 0.0
    turnover, fees, slippage = 0, 0.0, 0.0
    time_in_market = 0
    maximum_inventory = 0
    per_side = config.fee_ticks + config.slippage_ticks
    last_decision = len(rows) - config.delay - config.horizon
    for index in range(start, len(rows)):
        row = rows[index]
        # Exit processing precedes new decisions; partial exits have priority.
        if inventory and index >= exit_due:
            side = 1 if inventory > 0 else -1
            quantity = min(abs(inventory), row.bid_qty if side > 0 else row.ask_qty)
            quote = row.bid_price if side > 0 else row.ask_price
            cash += side * quantity * quote
            inventory -= side * quantity
            turnover += quantity
            fees = turnover * config.fee_ticks
            slippage = turnover * config.slippage_ticks
            assert open_trade is not None
            open_trade["exit_fills"].append({"index": index, "seq": row.seq,
                                              "price_ticks": quote, "quantity": quantity})
            if not inventory:
                open_trade["exit_index"] = index
                open_trade["net_pnl_ticks_lots"] = (cash - open_trade.pop("cash_before")
                                                    - (fees + slippage - open_trade.pop("costs_before")))
                trades.append(open_trade)
                open_trade = None
        if pending is not None and index == pending[0]:
            _, side, decision_index = pending
            quantity = min(config.order_size, row.ask_qty if side > 0 else row.bid_qty)
            quote = row.ask_price if side > 0 else row.bid_price
            open_trade = {"decision_index": decision_index, "decision_seq": rows[decision_index].seq,
                          "entry_index": index, "entry_seq": row.seq, "side": side,
                          "quantity": quantity, "entry_price_ticks": quote,
                          "cash_before": cash, "costs_before": fees + slippage, "exit_fills": []}
            cash -= side * quantity * quote
            inventory = side * quantity
            exit_due = index + config.horizon
            turnover += quantity
            fees = turnover * config.fee_ticks
            slippage = turnover * config.slippage_ticks
            pending = None
        if not inventory and pending is None and index < last_decision:
            side = decision(values[index], calibration)
            if side:
                pending = (index + config.delay, side, index)
        time_in_market += int(inventory != 0)
        maximum_inventory = max(maximum_inventory, abs(inventory))
        # Mid-marked equity is an accounting mark, never used as a fill.
        equity = (2 * cash + inventory * (row.bid_price + row.ask_price)) / 2.0 - fees - slippage
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        curve.append({"index": index, "seq": row.seq, "equity_ticks_lots": equity,
                      "inventory_lots": inventory, "drawdown_ticks_lots": drawdown})
    pnl = [trade["net_pnl_ticks_lots"] for trade in trades]
    gains, losses = sum(max(x, 0) for x in pnl), -sum(min(x, 0) for x in pnl)
    last = rows[-1]
    liquidation_quote = last.bid_price if inventory > 0 else last.ask_price
    # This is only a valuation bound: it is not booked as an executable fill.
    liquidation_mark = cash + inventory * liquidation_quote - fees - slippage - abs(inventory) * per_side
    return {
        "completed_round_trips": len(trades),
        "net_pnl_ticks_lots": curve[-1]["equity_ticks_lots"],
        "realized_round_trip_pnl_ticks_lots": sum(pnl),
        "max_drawdown_ticks_lots": max_drawdown,
        "turnover_lots": turnover,
        "fees_ticks_lots": fees,
        "slippage_ticks_lots": slippage,
        "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
        "mean_trade_pnl_ticks_lots": statistics.fmean(pnl) if pnl else None,
        "profit_factor": gains / losses if losses else None,
        "time_in_market_fraction": time_in_market / len(curve),
        "maximum_absolute_inventory_lots": maximum_inventory,
        "terminal_inventory_lots": inventory,
        "terminal_liquidation_mark_ticks_lots": liquidation_mark,
        "terminal_mark_warning": "Hypothetical quote valuation, not a fill; depth may be insufficient.",
        "open_trade": {key: value for key, value in open_trade.items()
                       if key not in ("cash_before", "costs_before")}
                      if open_trade is not None else None,
        "trades": trades,
        "equity_curve": curve,
    }


def evaluate(rows: Sequence[Snapshot], config: Config, *,
             fee_grid: Sequence[float] = (0.0, 0.1, 0.5, 1.0),
             delay_grid: Sequence[int] = (1, 2, 5)) -> dict:
    validate_snapshot_rows(rows)
    train_stop, split = split_indices(len(rows), config)
    for fee in fee_grid:
        replace(config, fee_ticks=fee).validate()
    for delay in delay_grid:
        split_indices(len(rows), replace(config, delay=delay))
    features = feature_series(rows, config.seed)
    result = {
        "schema_version": 1,
        "config": asdict(config),
        "units": {"price": "stored integer ticks", "quantity": "lots",
                  "pnl": "ticks * lots", "time": "valid two-sided BBO observations"},
        "split": {"total_observations": len(rows), "train_decision_start": 0,
                  "train_decision_stop_exclusive": train_stop,
                  "embargo_observations": config.delay + config.horizon,
                  "test_start": split, "test_start_seq": rows[split].seq,
                  "latest_train_label_index": train_stop - 1 + config.delay + config.horizon,
                  "test_decision_stop_exclusive": len(rows) - config.delay - config.horizon},
        "assumptions": [
            "Single chronological holdout; threshold quantile, horizon and costs are fixed inputs.",
            "Threshold and signal orientation use only training features and labels ending before test.",
            "Prices are stored ticks, not dollars or basis points; map units to your venue explicitly.",
            "Event delay is measured in exported observations, not sequence gaps or nanoseconds.",
            "Long entries cross ask; shorts cross bid; exits cross the opposite quote after the horizon.",
            "One pending entry/position; displayed top depth caps fills; partial exits continue on later rows.",
            "Displayed liquidity is assumed available; no market impact, queue position or hidden depth model.",
            "Historical book is exogenous: simulated trades do not alter subsequent observations.",
            "Shorting availability and funding are omitted; exchange fees/slippage are user-supplied tick costs.",
            "Equity/drawdown use midpoint accounting marks; fills never use midpoint.",
            "Microprice here is a weighted-mid proxy, not an estimated Markov-chain microprice.",
            "Microprice displacement equals half-spread times imbalance; these are not independent evidence.",
            "Random is a seeded uncalibrated-direction control; flat is the zero-turnover null.",
            "Overlapping labels are descriptive only; no IID significance or annualized performance claim.",
            "All cost/delay sensitivity scenarios freeze the base training calibration.",
            "Sensitivity panels are held-out diagnostics; choosing their winner requires a fresh holdout.",
        ],
        "strategies": {},
        "book_snapshots": [{"index": index, **asdict(rows[index])}
                           for index in range(split, len(rows))],
    }
    for name in SIGNALS:
        calibration = calibrate(features[name], rows, train_stop, config, name)
        simulation = simulate(rows, features[name], split, calibration, config)
        fee_sensitivity = []
        for fee in sorted(set([*fee_grid, config.fee_ticks])):
            scenario = simulate(rows, features[name], split, calibration, replace(config, fee_ticks=fee))
            fee_sensitivity.append({"fee_ticks_per_lot_per_side": fee,
                                    **compact_metrics(scenario)})
        delay_sensitivity = []
        for delay in sorted(set([*delay_grid, config.delay])):
            scenario_config = replace(config, delay=delay)
            scenario = simulate(rows, features[name], split, calibration, scenario_config)
            delay_sensitivity.append({"delay_observations": delay,
                                      "calibration_policy": "frozen_base_training_calibration",
                                      **compact_metrics(scenario)})
        result["strategies"][name] = {
            "train_calibration": asdict(calibration),
            "train_predictive": predictive_metrics(features[name], rows, 0, train_stop,
                                                     calibration, config),
            "test_predictive": predictive_metrics(features[name], rows, split,
                                                    len(rows) - config.delay - config.horizon,
                                                    calibration, config),
            "execution": simulation,
            "fee_sensitivity": fee_sensitivity,
            "delay_sensitivity": delay_sensitivity,
        }
    return result


def compact_metrics(simulation: dict) -> dict:
    return {key: simulation[key] for key in
            ("net_pnl_ticks_lots", "max_drawdown_ticks_lots", "completed_round_trips",
             "turnover_lots", "terminal_inventory_lots")}


def write_html(report: dict, path: Path) -> None:
    """Self-contained summary and replay inspector; JSON remains the audit record."""
    from research.replay_viewer import render_replay
    colors = ("#4dd9c0", "#ffba69", "#ae97ff", "#90a0b5", "#f3f5f7")
    curves = [item["execution"]["equity_curve"] for item in report["strategies"].values()]
    all_values = [point["equity_ticks_lots"] for curve in curves for point in curve]
    low, high = min(all_values + [0]), max(all_values + [0])
    span = high - low or 1
    paths = []
    for (name, item), color in zip(report["strategies"].items(), colors):
        curve = item["execution"]["equity_curve"]
        stride = max(1, len(curve) // 1500)
        sampled = list(range(0, len(curve), stride))
        if sampled[-1] != len(curve) - 1:
            sampled.append(len(curve) - 1)
        points = " ".join(f"{40 + i / max(1, len(curve) - 1) * 900:.2f},"
                          f"{280 - (curve[i]['equity_ticks_lots'] - low) / span * 240:.2f}"
                          for i in sampled)
        paths.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"><title>{name}</title></polyline>')
    def number(value: float | None) -> str:
        return "—" if value is None else f"{value:,.3f}"
    table_rows = []
    panels = []
    for (name, item), color in zip(report["strategies"].items(), colors):
        sim = item["execution"]
        table_rows.append(f'<tr><td style="color:{color}">{name}</td><td>{number(sim["net_pnl_ticks_lots"])}</td>'
                          f'<td>{number(sim["max_drawdown_ticks_lots"])}</td><td>{sim["completed_round_trips"]}</td>'
                          f'<td>{sim["turnover_lots"]}</td><td>{sim["terminal_inventory_lots"]}</td>'
                          f'<td>{number(item["test_predictive"]["feature_markout_correlation"])}</td></tr>')
        sensitivity = "".join(f'<tr><td>{row["fee_ticks_per_lot_per_side"]}</td><td>{number(row["net_pnl_ticks_lots"])}</td></tr>'
                              for row in item["fee_sensitivity"])
        fee_table = (f'<table><tr><th>Fee / side</th><th>Net PnL</th></tr>{sensitivity}</table>'
                     if sensitivity else '')
        panels.append(f'<details><summary>{name}: costs & calibration</summary>{fee_table}'
                      f'<pre>{html.escape(json.dumps(item["train_calibration"], indent=2))}</pre></details>')
    source = html.escape(report.get("source", {}).get("path", "in-memory data"))
    label = html.escape(report.get("source", {}).get("data_kind", "unspecified"))
    assumptions = "".join(f"<li>{html.escape(value)}</li>" for value in report["assumptions"])
    protocol_label = "Chronological holdout"
    cost_note = "Costs are ticks per lot per side, charged at entry and exit. JSON includes delay scenarios and every fill."
    if "fold" in report:
        protocol_label = html.escape(f"Walk-forward fold {report['fold']} (independent): test indices "
                                     f"[{report['split']['test_start']}, {report['split']['test_stop_exclusive']})")
        cost_note = "No fee/delay sensitivity scenarios were computed for this fold. JSON includes every recorded fill."
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order Book Research — Holdout Report</title><style>
body{{font:16px/1.55 system-ui;background:#101720;color:#e1e7ef;max-width:1100px;margin:auto;padding:30px}}h1{{font-size:38px;line-height:1.1}}h2{{margin-top:36px}}p,li{{color:#b9c6d8}}.badge{{color:#4dd9c0;letter-spacing:.12em;font-size:12px;text-transform:uppercase}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{text-align:right;padding:12px;border-bottom:1px solid #2a3544}}th:first-child,td:first-child{{text-align:left}}th{{color:#90a0b5;font-size:13px}}.scroll{{overflow-x:auto}}svg{{background:#17212f;width:100%;border-radius:10px}}details{{border:1px solid #2a3544;padding:15px;margin:12px 0;border-radius:8px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;font-size:12px}}code{{overflow-wrap:anywhere}}.warning{{padding:16px;border-left:3px solid #ffba69;background:#211f1c}}</style>
<div class="badge">C++ execution infrastructure × Python research</div><h1>Signals meet execution costs.</h1>
<p>{protocol_label} · {report['split']['total_observations']:,} BBO observations · data: <strong>{label}</strong></p>
<p class="warning">Research diagnostic, not evidence of tradable alpha. Synthetic fixtures validate plumbing and can contain mechanical patterns. Fees and event delay are scenario inputs.</p>
<p>Source: <code>{source}</code><br>Train decisions end before index {report['split']['train_decision_stop_exclusive']}; test begins at index {report['split']['test_start']}. Embargo: {report['split']['embargo_observations']} observations.</p>
<h2>Held-out equity · ticks × lots</h2><p>Midpoint accounting marks; all executions cross displayed quotes. Vertical range {number(low)} to {number(high)}. Horizontal axis is held-out observation order.</p>
<svg viewBox="0 0 980 320" role="img" aria-label="Held-out strategy equity curves">{''.join(paths)}</svg>
<div class="scroll"><table><tr><th>Strategy</th><th>Net PnL</th><th>Max drawdown</th><th>Round trips</th><th>Turnover</th><th>End inventory</th><th>Markout corr.</th></tr>{''.join(table_rows)}</table></div>
<h2>Transaction-cost diagnostics</h2><p>{cost_note}</p>{''.join(panels)}
{render_replay(report)}
<h2>Model contract & limitations</h2><ul>{assumptions}</ul><p>Reproduce with the accompanying JSON configuration and input SHA-256. No external assets, analytics, or network requests.</p></html>'''
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(document)


def validate_output_paths(input_path: Path, output_path: Path,
                          html_path: Path | None = None) -> None:
    """Keep source data and previous research evidence intact."""
    outputs = [output_path] + ([html_path] if html_path is not None else [])
    resolved = [input_path.resolve(), *(path.resolve() for path in outputs)]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Input, JSON output and HTML output must use distinct paths")
    for path in outputs:
        if path.exists() or path.is_symlink():
            raise ValueError(f"Output already exists: {path}; use a new path")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="replay BBO CSV")
    parser.add_argument("--output", required=True, type=Path, help="new machine-readable JSON report (never overwritten)")
    parser.add_argument("--html", type=Path, help="new self-contained HTML summary (never overwritten)")
    parser.add_argument("--data-kind", required=True, choices=("synthetic", "historical"),
                        help="provenance declaration; historical does not certify source quality")
    parser.add_argument("--train-fraction", type=float, default=0.6)
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--threshold-quantile", type=float, default=0.75)
    parser.add_argument("--fee-ticks", type=float, default=0.1,
                        help="fee per lot per side in stored ticks")
    parser.add_argument("--slippage-ticks", type=float, default=0.0,
                        help="additional adverse slippage per lot per side in stored ticks")
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fee-grid", type=float, nargs="+", default=[0, 0.1, 0.5, 1])
    parser.add_argument("--delay-grid", type=int, nargs="+", default=[1, 2, 5])
    args = parser.parse_args(argv)
    config = Config(**{key: getattr(args, key) for key in Config.__dataclass_fields__})
    try:
        validate_output_paths(args.input, args.output, args.html)
        rows, source_sha256 = read_snapshot_source(args.input)
        report = evaluate(rows, config, fee_grid=args.fee_grid, delay_grid=args.delay_grid)
        report["source"] = {"path": str(args.input), "data_kind": args.data_kind,
                            "sha256": source_sha256}
        report["runtime"] = {"python": sys.version.split()[0], "research_version": 1}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        document = json.dumps(report, indent=2, allow_nan=False) + "\n"
        # Exclusive creation also protects a file created after preflight.
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(document)
        if args.html:
            write_html(report, args.html)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Wrote {args.output}: {len(rows)} observations; data_kind={args.data_kind}")
    for name, item in report["strategies"].items():
        sim = item["execution"]
        print(f"{name:12s} PnL={sim['net_pnl_ticks_lots']:12.3f} ticks*lots  "
              f"round_trips={sim['completed_round_trips']}  terminal_inventory={sim['terminal_inventory_lots']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
