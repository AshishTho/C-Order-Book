"""Deliberately broken: a centered mean includes the next close at the current row."""


def decision(close):
    return (close > close.rolling(3, center=True).mean()).astype(int)
