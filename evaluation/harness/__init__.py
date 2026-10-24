"""KAN-133: no-cost, no-fault skeleton of the KAN-58 60x4 evaluation harness.

Stdlib only. Nothing here calls an LLM, kubectl or reads a Secret; real fault
workers and inference runners are interfaces to be supplied by KAN-58 later.
"""
HARNESS_VERSION = "0.1.0-kan133"
