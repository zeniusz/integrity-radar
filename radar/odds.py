"""Price conversion helpers."""
import math


def american_to_prob(s):
    if s is None:
        return None
    s = str(s).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        o = float(s)
    except ValueError:
        return None
    if o == 0:
        return None
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def devig2(a, b):
    """Remove bookmaker margin from a two-way market."""
    if a is None or b is None or a + b == 0:
        return None, None
    return a / (a + b), b / (a + b)


def decimal_devig(odds):
    inv = []
    for o in odds:
        try:
            o = float(o)
        except (TypeError, ValueError):
            return None
        if o <= 1:
            return None
        inv.append(1 / o)
    t = sum(inv)
    return [x / t for x in inv], t - 1  # probabilities, overround


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def effective_pp(p_from, p_to):
    """A move expressed as the pp it would be at 50%, so 85%->90% counts more than 50%->55%.

    Uses the log-odds change scaled by the slope of the logistic curve at 0.5 (0.25).
    """
    return (logit(p_to) - logit(p_from)) * 25.0
