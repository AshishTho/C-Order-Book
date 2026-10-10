"""Fix this window: use a trailing mean, assuming past-to-future rows.

The current close must still be available before deciding. Warm-up rows, fills,
feature availability and other leakage risks require separate review.
"""


def decision(close):
    return (close > close.rolling(3, center=False).mean()).astype(int)
