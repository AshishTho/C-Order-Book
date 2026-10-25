"""Dependency-free inspection of recorded simulation results, never a live executor."""
from __future__ import annotations

import json


MAX_SAVED_REPORT_BYTES = 64 * 1024 * 1024


def _validate_saved_cost_inputs(report):
    """Reject numeric-looking strings before fee arithmetic or browser accumulation."""
    import math
    for field in ("fee_ticks", "slippage_ticks"):
        value = report["config"][field]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"Saved {field} must be a finite nonnegative number")
    for item in report["strategies"].values():
        execution = item["execution"]
        trades = list(execution["trades"])
        if execution.get("open_trade") is not None:
            trades.append(execution["open_trade"])
        for trade in trades:
            for quantity in [trade["quantity"], *(fill["quantity"] for fill in trade["exit_fills"])]:
                if type(quantity) is not int or not 0 < quantity < 2**63:
                    raise ValueError("Saved entry/exit quantity must be a positive integer below 2**63")


def select_saved_report(document: dict, fold: int | None = None) -> dict:
    """Select recorded results only; never read source data or recalculate a fold."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Expected a saved version-1 research report")
    if "folds" in document:
        if document.get("protocol") != "expanding_history_independent_folds":
            raise ValueError("Unsupported fold report protocol")
        folds = document["folds"]
        if not isinstance(folds, list) or type(fold) is not int or not 0 <= fold < len(folds):
            raise ValueError("A walk-forward report requires --fold with a valid zero-based index")
        selected = folds[fold]
        if not isinstance(selected, dict) or selected.get("fold") != fold:
            raise ValueError("Selected fold identity is inconsistent")
        selected = selected.copy()
        selected.setdefault("source", document.get("source", {}))
    else:
        if fold is not None:
            raise ValueError("--fold applies only to a saved walk-forward report")
        selected = document
    if not isinstance(selected.get("strategies"), dict) or not selected["strategies"]:
        raise ValueError("Saved report has no recorded strategies")
    return selected


def export_saved_report(source, output, *, fold=None):
    """Publish a complete, no-clobber replay HTML from at most 64 MiB of saved JSON."""
    import hashlib
    import html
    import os
    from pathlib import Path
    import tempfile

    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("Input and output must use distinct paths")
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new path")
    with source.open("rb") as stream:
        raw = stream.read(MAX_SAVED_REPORT_BYTES + 1)
    if len(raw) > MAX_SAVED_REPORT_BYTES:
        raise ValueError("Saved report exceeds the 64 MiB limit")
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    document = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant,
                          object_pairs_hook=unique_keys)
    selected = select_saved_report(document, fold)
    try:
        _validate_saved_cost_inputs(selected)
        fragment = render_replay(selected)
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError) as exc:
        raise ValueError("Incomplete or invalid saved replay fields") from exc
    evidence = {"saved_report": str(source), "saved_report_sha256": hashlib.sha256(raw).hexdigest(),
                "selected_fold": fold,
                "recorded_metadata": {key: document[key] for key in
                    ("source", "provenance", "runtime", "config", "protocol", "limitations") if key in document}}
    metadata = html.escape(json.dumps(evidence, indent=2, allow_nan=False))
    label = "Saved chronological holdout" if fold is None else f"Saved walk-forward fold {fold}"
    rendered = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{label} replay</title>
<style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:auto;padding:24px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px;border-bottom:1px solid #bbb;text-align:left}}.scroll{{overflow-x:auto}}
.warning{{border-left:3px solid #aa6800;padding:12px;background:#fff6df}}</style>
<h1>{label}</h1><p>This export reads saved results only. It does not rerun calibration,
simulation or trading, and does not open the original CSV. Recorded provenance declarations
are shown below; their original inputs and manifests have not been reverified.</p>
{fragment}<details><summary>Saved report identity and recorded metadata</summary><pre>{metadata}</pre></details>
</html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".saved-replay-", dir=output.parent) as folder:
        staged = Path(folder) / "replay.html"
        staged.write_text(rendered, encoding="utf-8")
        # Publish only after the renderer and write succeed; never replace a competitor.
        os.link(staged, output)
    return evidence


def main(argv=None):
    import argparse
    from pathlib import Path
    import sys
    parser = argparse.ArgumentParser(description="Inspect saved research JSON without rerunning the experiment.")
    parser.add_argument("--input", required=True, type=Path, help="saved lab or walk-forward JSON, up to 64 MiB")
    parser.add_argument("--output", required=True, type=Path, help="new self-contained replay HTML; never overwritten")
    parser.add_argument("--fold", type=int, help="required zero-based fold for a walk-forward report")
    args = parser.parse_args(argv)
    try:
        export_saved_report(args.input, args.output, fold=args.fold)
    except (ValueError, OSError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output} from recorded results only")
    return 0


def replay_payload(report: dict) -> dict:
    config = report["config"]
    strategies = {}
    book, issues = [], []
    issue_count = 0
    def add_issue(message):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 100:
            issues.append(message)
    previous_index = previous_seq = -1
    fields = ("index", "seq", "bid_price", "ask_price", "bid_qty", "ask_qty")
    recorded_book = report.get("book_snapshots", [])
    if not isinstance(recorded_book, list):
        add_issue("Book snapshots must be a list")
        recorded_book = []
    for position, row in enumerate(recorded_book):
        if not isinstance(row, dict) or any(type(row.get(key)) is not int for key in fields):
            add_issue(f"Book row {position}: missing or non-integer fields")
            continue
        index, seq, bid, ask, bid_qty, ask_qty = (row[key] for key in fields)
        if index <= previous_index or seq <= previous_seq or index < 0 or not 0 <= seq < 2**64:
            add_issue(f"Book row {position}: invalid observation or sequence order")
        if not 0 < bid < ask < 2**63 or not 0 < min(bid_qty, ask_qty) <= max(bid_qty, ask_qty) < 2**63:
            add_issue(f"Book row {position}: invalid prices or displayed quantities")
        previous_index, previous_seq = index, seq
        book.append([index, str(seq), str(bid), str(ask), str(bid_qty), str(ask_qty)])
    book_by_index = {row[0]: row for row in book}
    for name, item in report["strategies"].items():
        simulation = item["execution"]
        fills = []
        trades = list(simulation["trades"])
        if simulation.get("open_trade") is not None:
            trades.append(simulation["open_trade"])
        for trade_number, trade in enumerate(trades, 1):
            entry_side = "Buy" if trade["side"] > 0 else "Sell"
            records = [(trade["entry_index"], trade["entry_seq"], "Entry", entry_side,
                        trade["entry_price_ticks"], trade["quantity"], None)]
            records.extend((fill["index"], fill["seq"], "Exit",
                            "Sell" if trade["side"] > 0 else "Buy", fill["price_ticks"],
                            fill["quantity"], trade.get("net_pnl_ticks_lots")
                            if n == len(trade["exit_fills"]) - 1 else None)
                           for n, fill in enumerate(trade["exit_fills"]))
            for index, seq, kind, side, price, quantity, outcome in records:
                # Decimal strings keep large integer prices, sequences and lots exact in JS.
                fills.append([index, str(seq), kind, side, str(price), str(quantity),
                              quantity * config["fee_ticks"], quantity * config["slippage_ticks"],
                              trade_number, outcome])
        fills.sort(key=lambda row: row[0])
        if "book_snapshots" in report:
            for point in simulation["equity_curve"]:
                quote = book_by_index.get(point["index"])
                if quote is None or quote[1] != str(point["seq"]):
                    add_issue(f"{name}: missing or mismatched book at observation {point['index']}")
            for fill in fills:
                quote = book_by_index.get(fill[0])
                if (quote is None or quote[1] != fill[1] or int(fill[5]) <= 0
                        or fill[4] != quote[3 if fill[3] == "Buy" else 2]
                        or int(fill[5]) > int(quote[5 if fill[3] == "Buy" else 4])):
                    add_issue(f"{name}: fill disagrees with recorded quote at observation {fill[0]}")
        strategies[name] = {
            "points": [[p["index"], str(p["seq"]), p["equity_ticks_lots"],
                        str(p["inventory_lots"]), p["drawdown_ticks_lots"]]
                       for p in simulation["equity_curve"]],
            "fills": fills,
            "totalFees": simulation["fees_ticks_lots"],
            "totalSlippage": simulation["slippage_ticks_lots"],
            "realizedPnl": simulation["realized_round_trip_pnl_ticks_lots"],
            "terminalInventory": str(simulation["terminal_inventory_lots"]),
            "terminalLiquidationMark": simulation["terminal_liquidation_mark_ticks_lots"],
        }
    source = report.get("source", {})
    return {"source": {k: source.get(k) for k in ("path", "sha256", "data_kind")},
            "config": config, "split": report["split"], "units": report["units"],
            "strategies": strategies, "book": book,
            "invariants": {"bookRecorded": "book_snapshots" in report,
                           "checkedObservations": len(book), "issues": issues,
                           "issueCount": issue_count}}


def render_replay(report: dict) -> str:
    payload = json.dumps(replay_payload(report), separators=(",", ":"), allow_nan=False)
    # A source path or strategy label must never terminate the JSON script element.
    for character, replacement in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                                   ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        payload = payload.replace(character, replacement)
    return r'''
<section id="replay"><h2>Inspect the recorded replay</h2>
<p>Observation order, not clock time. This reads the recorded simulation; controls do not rerun a strategy or place orders. Losing results remain visible.</p>
<label for="replayStrategy">Strategy</label> <select id="replayStrategy"></select>
<p><button id="previousFill" type="button">Previous fill</button> <button id="nextFill" type="button">Next fill</button></p>
<label for="replayCursor">Held-out observation</label><input id="replayCursor" type="range" min="0" value="0" style="width:100%">
<p id="replayPosition" role="status"></p><p id="replayState"></p><p id="replayCosts"></p>
<p id="replayBook"></p><p id="replayInvariant" class="warning" role="alert"></p>
<p>Recorded top of book and fill timeline only; original order-level events are not reconstructed. Legacy reports without book snapshots say so explicitly. These bounded consistency checks do not certify every engine invariant.</p>
<div class="scroll"><table><thead><tr><th>Trade</th><th>Stage / side</th><th>Sequence</th><th>Price ticks</th><th>Lots</th><th>Fee</th><th>Slippage</th><th>Completed round-trip PnL</th></tr></thead><tbody id="replayFills"></tbody></table></div>
<p id="replayEmpty"></p><p id="replayTerminal" class="warning"></p>
<p>Per-fill fee and slippage are derived from recorded quantity and the saved per-side cost configuration. Equity is a midpoint accounting mark; it is not a fill price. A terminal quote liquidation mark is hypothetical and never added to the fill list.</p>
<details><summary>Source provenance & exact experiment configuration</summary><pre id="replayProvenance"></pre></details>
</section>
<script id="replayData" type="application/json">''' + payload + r'''</script>
<script>
(()=>{
 'use strict';
 const data=JSON.parse(document.getElementById('replayData').textContent),get=id=>document.getElementById(id);
 const select=get('replayStrategy'),cursor=get('replayCursor');
 const bookByIndex=new Map(data.book.map(row=>[row[0],row]));
 get('replayInvariant').textContent=!data.invariants.bookRecorded?'Book state was not recorded; book invariants were not checked.':data.invariants.issueCount?'Invariant failures ('+data.invariants.issueCount+'; showing up to 100): '+data.invariants.issues.join('; '):'Recorded book consistency checks passed for '+data.invariants.checkedObservations+' observations.';
 const number=value=>value===null?'Not recorded':Number(value).toLocaleString(undefined,{maximumFractionDigits:6});
 for(const name of Object.keys(data.strategies)){const option=document.createElement('option');option.value=name;option.textContent=name;select.append(option);}
 get('replayProvenance').textContent=JSON.stringify({source:data.source,configuration:data.config,split:data.split,units:data.units},null,2);
 let active=null,fees=[],slippage=[];
 function render(){
  const point=active.points[Number(cursor.value)];if(!point){get('replayPosition').textContent='No recorded observations.';return;}
  const index=point[0];get('replayPosition').textContent='Observation index '+index+' · sequence '+point[1];
  get('replayState').textContent='Midpoint equity: '+number(point[2])+' ticks × lots · inventory: '+point[3]+' lots · drawdown: '+number(point[4]);
  let lo=0,hi=active.fills.length;while(lo<hi){const mid=(lo+hi)>>1;if(active.fills[mid][0]<=index)lo=mid+1;else hi=mid;}
  const end=lo;let start=end;while(start>0&&active.fills[start-1][0]===index)start--;
  get('replayCosts').textContent='Recorded costs through this observation: fees '+number(fees[end])+' · slippage '+number(slippage[end])+' ticks × lots.';
  const body=get('replayFills');body.replaceChildren();
  const quote=bookByIndex.get(index);get('replayBook').textContent=quote?'Recorded top of book at sequence '+quote[1]+': bid '+quote[2]+' ticks / '+quote[4]+' lots; ask '+quote[3]+' ticks / '+quote[5]+' lots.':'Top of book not recorded at this observation.';
  for(const f of active.fills.slice(start,end)){const tr=document.createElement('tr');for(const value of [f[8],f[2]+' / '+f[3],f[1],f[4],f[5],number(f[6]),number(f[7]),f[9]===null?'—':number(f[9])]){const td=document.createElement('td');td.textContent=value;tr.append(td);}body.append(tr);}
  get('replayEmpty').textContent=end===start?'No recorded fills at this observation.':'';
  get('previousFill').disabled=!active.fills.some(f=>f[0]<index);get('nextFill').disabled=!active.fills.some(f=>f[0]>index);
 }
 function choose(){active=data.strategies[select.value];fees=[0];slippage=[0];for(const f of active.fills){fees.push(fees.at(-1)+f[6]);slippage.push(slippage.at(-1)+f[7]);}cursor.max=Math.max(0,active.points.length-1);cursor.value=0;
  get('replayTerminal').textContent='End of recorded data: inventory '+active.terminalInventory+' lots; completed round-trip PnL '+number(active.realizedPnl)+'; total fees '+number(active.totalFees)+'; total slippage '+number(active.totalSlippage)+'. Hypothetical terminal liquidation mark: '+number(active.terminalLiquidationMark)+' ticks × lots (not a booked fill; depth may be insufficient).';render();}
 function jump(direction){const index=active.points[Number(cursor.value)]?.[0];const fill=direction>0?active.fills.find(f=>f[0]>index):active.fills.findLast(f=>f[0]<index);if(!fill)return;const position=active.points.findIndex(p=>p[0]===fill[0]);if(position>=0){cursor.value=position;render();}}
 select.addEventListener('change',choose);cursor.addEventListener('input',render);get('previousFill').onclick=()=>jump(-1);get('nextFill').onclick=()=>jump(1);if(select.options.length)choose();
})();
</script>
'''


if __name__ == "__main__":
    raise SystemExit(main())
