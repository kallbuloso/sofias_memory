"""Concrete pipeline step implementations (ADR-0009 SS O).

Thin adapters between the engine's three-phase step contract and the
business services that own the actual algorithms. A step here never invents
its own dependencies: everything external arrives through
``PipelineContext.resources``, populated once at application startup.
"""
