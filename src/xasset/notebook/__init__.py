"""Model-based strategies from the experimental research notebook.

Each study builds a prior-only signal, compares a return model with and without it
(incremental predictive value), selects thresholds inside training windows only,
and trades the fixed rule on later, purged walk-forward folds. The final 20% of a
registered study is a sealed holdout, as for state-machine books.
"""
