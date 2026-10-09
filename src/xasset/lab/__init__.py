"""Strategy lab: one causal runtime for historical replay and live paper trading.

The lab runs registered state-machine strategies over one-minute bars that may carry
quote midpoints and aggressor-labelled flow. Historical replays and the live paper
desk share the same feature, strategy, sizing and portfolio code; only the execution
model differs (bar-open approximation versus live executable quotes).
"""
