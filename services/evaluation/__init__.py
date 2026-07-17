"""Offline evaluation contracts (Stage 8): versioned gold datasets, pure loaders, validators.

Nothing in this package touches a database, a network, spaCy, or an LLM. A gold dataset is
committed as JSON, loaded and validated deterministically, and converted into the Stage-9
input DTOs on demand. Import the concrete contract from its module, e.g.
``services.evaluation.entity_linking_gold``.
"""
