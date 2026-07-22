"""Authoritative workflow definitions (dependency graphs and metadata).

Modules under this package own domain workflow *definitions* — the capability
dependency graphs and hash-identified workflow metadata. Capability, worker, and
executor modules attach behavior to the capability IDs declared here. The generic
``WorkflowRunner`` schedules these graphs but does not import them.
"""
