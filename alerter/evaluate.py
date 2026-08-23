"""Turning a sample into a state."""

HEALTHY = "healthy"
WARNING = "warning"
ERROR = "error"


def resolve_value(rule, samples):
    """The comparable number for a rule, or None when it cannot be computed.

    A rule with `of` divides one metric by another: the collector stores
    absolute bytes, while thresholds are written as percentages.
    """
    value = samples.get(rule.metric)
    if value is None:
        return None
    if rule.of is None:
        return float(value)
    total = samples.get(rule.of)
    if total is None or float(total) == 0.0:
        return None
    return 100.0 * float(value) / float(total)


def _breached(direction, value, threshold):
    if threshold is None:
        return False
    return value >= threshold if direction == "above" else value <= threshold


def classify(rule, value):
    if _breached(rule.direction, value, rule.error):
        return ERROR
    if _breached(rule.direction, value, rule.warning):
        return WARNING
    return HEALTHY
