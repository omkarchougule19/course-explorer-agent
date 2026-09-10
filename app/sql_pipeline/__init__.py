"""
sql_pipeline

An explicit Generator -> Critic -> Repair text-to-SQL pipeline, wired with
LangGraph, that sits alongside the production `create_sql_agent` path in
app/agent.py. It exists to be measured: evals/ compares answer quality with
the Critic/Repair loop on vs. off. See DECISIONS.md ("Critic/Repair loop for
the text-to-SQL agent") for why this is a separate pipeline rather than a
wrapper around the existing agent.

Entry point: `run_pipeline(question, mode=..., history=...)` in graph.py.
"""

from app.sql_pipeline.graph import run_pipeline, PipelineResult

__all__ = ["run_pipeline", "PipelineResult"]
