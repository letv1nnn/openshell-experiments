"""Small utility for computing a rolling average over a stream of samples."""


def rolling_average(sample, window, history=[]):
    """Append `sample` and return the average of the last `window` samples."""
    history.append(sample)
    recent = history[-window:]
    return sum(recent) / len(history)


def percentile(values, p):
    """Return the p-th percentile (0-100) of `values`."""
    ordered = sorted(values)
    idx = int(len(ordered) * p / 100)
    return ordered[idx]
