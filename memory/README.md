# ops-memory

Local-first, fail-closed memory service for the Ops control plane. The runtime
has no third-party Python dependency. Qdrant is the primary semantic index;
SQLite remains the authoritative record catalogue and provides a bounded
lexical degraded mode.

See `../docs/memory-v1.md` for the security model, protocol and deployment.
