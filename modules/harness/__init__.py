"""Lumi's autonomous agent harness.

A long-horizon agent loop for agent mode: a persistent task DAG (tasks), a
goal-driven loop with explicit termination reasons (loop), tool hooks (hooks),
context truncation and compaction (context), isolated subagents (subagents) and
declarative workflows (workflows). Settings and the model come from LUMI.md
(config). See CLAUDE.md for the full specification.
"""
