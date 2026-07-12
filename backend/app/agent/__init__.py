"""LLM-driven data-analyst agent.

An agent run populates a workspace after upload: it profiles the tables,
infers the audit domain, creates high-confidence joins, proposes validation
rules, runs library and custom analytics, curates dashboard tiles, and writes
an evidence-linked summary — either autonomously (auto mode) or pausing for
editable approval of every mutation batch (permission mode).

Modules:
    store    durable run persistence (run.json + events.jsonl per run)
    suggest  deterministic, profile-based validation-rule suggestions
    joins    deterministic join-candidate inference and diagnostics
    prompts  LLM prompt builders and JSON-response parsing
    runner   the run state machine, worker thread, and control surface
    summary  findings assembly and markdown rendering
"""
