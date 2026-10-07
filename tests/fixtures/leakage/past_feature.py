"""Fix this particular feature: use a prior close, assuming past-to-future rows.

The current close still must be available before deciding; execution timing and
all other leakage risks require a separate review. This is not a full strategy.
"""


def decision(close):
    return (close > close.shift(1)).astype(int)
