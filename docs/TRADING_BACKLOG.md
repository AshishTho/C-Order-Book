# Bounded trading research backlog

Continue in order using isolated branches and draft pull requests. Check fresh,
verified account usage before every batch; stop with headroom above the user's
50% weekly remaining floor. Do not merge, deploy, access brokers or place trades.
Keep negative outcomes, exact input hashes, tests and exact-commit CI evidence.

| Item | Status | Scope / next action |
| --- | --- | --- |
| 12 Replay viewer | Existing; reused and regression-tested | Existing `research/replay_viewer.py`; walk-forward renders a selected fold through the same viewer. |
| 13 Walk-forward evaluation | Implemented in this branch | Fixed inputs, expanding history, purged labels, complete non-overlapping test blocks and independent fold accounting. Seven focused chronology/report tests; exact-commit CI belongs in the draft PR. |
| 14 Reproducible experiment manifests | Pending next admitted batch | Extend existing manual Repro Capsule with explicit experiment parameters and exact evidence hashes. Preserve preview/hash confirmation, inert commands, no credential capture, and overwrite refusal. |
| 15 Execution stress scenarios | Pending after 14 | Explicit spread/fee/delay/depth scenarios with frozen training calibration, preserved partial inventory and losing outcomes. Diagnostic scenarios cannot become claims of fresh holdout performance. |

Current research code remains synthetic and does not establish live tradable
alpha. Resume pending items only after the shared budget is checked again.
