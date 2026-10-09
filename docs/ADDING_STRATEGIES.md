# Adding a strategy

There are two kinds of strategy in this project. Pick the one that matches how the
idea is written.

| The idea is… | Use | Runs on |
|---|---|---|
| a sequence of conditions on minute bars (arm, wait, confirm, stop) | a state-machine strategy (`src/xasset/lab/strategies/`) | replay of archives and the live paper desk |
| a signal plus a forecast model, judged against a model without the signal | a notebook strategy (`src/xasset/notebook/`) | registered walk-forward studies |

## State-machine strategy

1. **Write the class** in `src/xasset/lab/strategies/hNN_name.py`:

   ```python
   from xasset.lab.strategy import Candidate, Requirements, Setup, Strategy


   class MyIdea(Strategy):
       id = "h11"  # stable ID: used in books, ledgers, the UI
       title = "One-line name"
       direction = 1  # -1 for shorts (perpetuals only)
       kinds = ("spot", "equity", "etf")
       time_exit_minutes = 60
       requires = Requirements(quotes=True, flow=True, notes="What the data must contain.")

       def arm(self, symbol: str) -> Setup | None:
           # Read prior-only features from self.market at minute self.minute.
           # Return self.new_setup(symbol, "STATE", anchor=value) to arm.
           ...

       def step(self, setup: Setup) -> Candidate | None:
           # Advance the setup; self.expire(setup, "reason") to drop it;
           # return self.candidate(setup, stop=..., sigma10=...) to confirm.
           ...
   ```

   Useful pieces: `self.market.tapes[symbol]` (`close`, `ret`, `flow`, `sum_notional`),
   `self.market.sigma(symbol, h, i, price)`, `self.market.quantile(symbol, "R10", i, p)`,
   `self.market.breadth(...)`, `self.market.residual_sum(...)`. Calibrations are
   prior-only by construction; never read a tape beyond `self.minute`.
2. **Register it** in `src/xasset/lab/strategies/__init__.py` (`REGISTRY`) and describe
   it in `src/xasset/lab/catalog.py` (`ENTRIES`: data needs, where it can run, why).
3. **Test it** in `tests/test_lab_strategies.py` with the `Harness` from
   `tests/lab_helpers.py`: one path that confirms, one that expires, one boundary case.
   `tests/test_lab_leakage.py` replays every registered strategy with future data
   mutated; add the new ID to its `STRATEGIES` list.
4. **Add it to a book** (`config/lab/*.yaml`, `strategies:` in priority order) and
   register a new book ID — changing a registered book requires a new ID:

   ```bash
   uv run --frozen xasset lab register config/lab/my-book.yaml --start 2026-01-01T00:00Z --end 2026-10-01T00:00Z
   uv run --frozen xasset lab run config/lab/my-book.yaml
   ```
5. **Paper trade it** by adding the ID to `config/lab/crypto-paper-trade.yaml` (archive
   variant) or `crypto-paper-quote.yaml` (quote version) and restarting the desk.

## Notebook strategy

1. **Write the class** in `src/xasset/notebook/nNN_name.py` with:
   - `id`, `title`, `horizon` (days) and `rule = Rule(horizon=..., target=..., stop=...)`;
   - `prepare(context)`: compute everything that needs no fitting (prior-only);
   - `signals(context, fit_until)`: return `{"S": array, ...}` using models fitted only
     on rows whose outcome is known before `fit_until`;
   - `outcome(context)`: the realized gross log return of the trade rule per row;
   - `precondition(context, signals)`: the strategy's own entry condition;
   - `exit_mask(context, signals, qualifies)`: where to leave early (or `None`);
   - `diagnostics(context, signals, rows)`: the rejection checks the idea calls for.
2. **Register it** in `STRATEGIES` in `src/xasset/notebook/study.py` and in
   `NOTEBOOK_IMPLEMENTED` plus `ENTRIES` in `src/xasset/lab/catalog.py`.
3. **Test it** in `tests/test_notebook.py`: label construction on a hand-made path, and
   the future-mutation test (decisions on the first test day must not change when
   later prices change).
4. **Create a study** YAML (copy `config/lab/notebook-crypto-daily.yaml`, new `id`) and
   register and run it with `xasset study register` / `xasset study run`.

The walk-forward framework (`walkforward.py`) handles purging, nested selection of the
ridge penalty and the signal threshold, the controls-only comparison, matched
baselines, cost and delay stress and the dashboard output.
