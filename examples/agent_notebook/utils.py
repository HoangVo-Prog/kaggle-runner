def score(predictions, targets):
    if len(predictions) != len(targets) or not targets:
        raise ValueError('Expected nonempty equal length arrays')
    return sum(int(p == t) for p, t in zip(predictions, targets)) / len(targets)
