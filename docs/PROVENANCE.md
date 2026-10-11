# Source provenance

This organization repository is a curated release candidate with independent Git history. It is not a mirror or fork of the private source repository.

Private reference: abhinavmusrif/megasmoves
Reference commit: b8ac7a5d3d04a9b8c6387c857057f1775fabd603 (October 7, 2026)

## Files selected from internal code

- worker/megas/bounded_io.py -> src/megas_community/bounded_io.py
- worker/megas/terminal_parse.py -> src/megas_community/terminal_parse.py
- worker/megas/mcp/secret_policy.py -> src/megas_community/secret_policy.py

These specific modules were inspected for common credential patterns before transfer. This was not a comprehensive audit or third-party licensing certification.

The human-approved workspace registry, CLI, read-only MCP preview and their tests are newly implemented as a minimal public-source candidate. They do **not** represent the entire internal operator implementation.

Every later source import must carry source path and commit, copyright/dependency review, secret scanning, reproducible tests and owner approval. Do not transfer the internal repository's Git history, production deployment configuration, runtime state or customer data.
