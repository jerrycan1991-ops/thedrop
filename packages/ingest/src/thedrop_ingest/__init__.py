"""VPS-side ingestion: normalization and cheap deduplication.

Everything here runs on the VPS and uses no model of any kind (PIPELINE.md 3-4).
That is deliberate: embeddings and clustering are the desktop's job (ADR-0005), and
this stage has to run in single-digit milliseconds on a 4-core box shared with a
hosting panel.
"""
