"""Deliberately broken: the decision uses the next observation's close."""


def decision(close):
    return (close.shift(-1) > close).astype(int)
