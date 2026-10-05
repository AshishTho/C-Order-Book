"""Generate SYNTHETIC normalized L3 events for the C++ replay application.

This small stochastic fixture tests a data pipeline. It is not an exchange
simulator, empirically calibrated model, or source of evidence about alpha.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
from typing import Iterator, Sequence


EVENT_SCHEMA = ("seq", "type", "order_ref", "side", "price", "qty")


def generate_events(count: int = 10000, seed: int = 42) -> Iterator[dict]:
    """Yield valid noncrossing A/E/X/D events with unique external references.

    Two permanent deep quotes ensure a two-sided book after initialization;
    a near quote on each side is canceled/replaced or partially depleted.
    Anchor exposure and asynchronous replacement create artificial patterns.
    Prices are integer engine ticks, quantities are integer lots.
    """
    if count < 4:
        raise ValueError("events must be at least 4")
    rng = random.Random(seed)
    seq, next_ref = 0, 1
    near: dict[str, dict | None] = {"B": None, "S": None}
    for side, price, quantity in (("B", 9900, 10000), ("S", 10100, 10000),
                                  ("B", 9999, 100), ("S", 10001, 100)):
        seq += 1
        event = dict(zip(EVENT_SCHEMA, (seq, "A", next_ref, side, price, quantity)))
        if seq >= 3:
            near[side] = {"order_ref": next_ref, "price": price, "qty": quantity}
        next_ref += 1
        yield event
    center = 10000
    while seq < count:
        seq += 1
        if rng.random() < 0.15:
            center = max(9950, min(10050, center + rng.choice((-1, 1))))
        missing = [side for side in ("B", "S") if near[side] is None]
        side = missing[0] if missing else rng.choice(("B", "S"))
        order = near[side]
        if order is None:
            opposite = near["S" if side == "B" else "B"]
            width = rng.choice((1, 1, 1, 2, 3))
            if side == "B":
                far_price = opposite["price"] if opposite else 10100
                price = min(center - width, far_price - 1)
            else:
                far_price = opposite["price"] if opposite else 9900
                price = max(center + width, far_price + 1)
            quantity = rng.randint(10, 300)
            order = {"order_ref": next_ref, "price": price, "qty": quantity}
            near[side] = order
            next_ref += 1
            yield dict(zip(EVENT_SCHEMA, (seq, "A", order["order_ref"], side, price, quantity)))
        elif rng.random() < 0.4:
            near[side] = None
            yield dict(zip(EVENT_SCHEMA, (seq, "D", order["order_ref"], "-", 0, 0)))
        else:
            quantity = rng.randint(1, order["qty"])
            order["qty"] -= quantity
            kind = "E" if rng.random() < 0.55 else "X"
            if order["qty"] == 0:
                near[side] = None
            yield dict(zip(EVENT_SCHEMA, (seq, kind, order["order_ref"], "-", 0, quantity)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    if args.events < 4:
        parser.error("--events must be at least 4")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_SCHEMA)
        writer.writeheader()
        writer.writerows(generate_events(args.events, args.seed))
    metadata = {
        "data_kind": "synthetic", "generator": "research.generate", "generator_version": 1,
        "seed": args.seed, "events": args.events,
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "warning": "Toy stochastic fixture, not calibrated market data or evidence of alpha.",
        "price_units": "integer engine ticks", "quantity_units": "integer lots",
    }
    meta_path = args.output.with_suffix(args.output.suffix + ".meta.json")
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.events} SYNTHETIC events to {args.output}; seed={args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
