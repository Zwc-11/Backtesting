"""Live paper desk: public market-data feeds, minute bars, quote fills and persistence.

Nothing in this package sends orders anywhere. Fills are simulated against the bid
and ask received from public websocket feeds, under the same runtime, strategies,
sizing and limits as historical replay.
"""
