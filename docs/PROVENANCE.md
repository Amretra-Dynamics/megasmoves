# Source provenance

This organization repository is an independently curated source release candidate. Its history does not include the internal development history.

The following modules were individually selected from the private MegasMoves implementation as of October 7, 2026:

- worker/megas/bounded_io.py -> src/megas_community/bounded_io.py
- worker/megas/terminal_parse.py -> src/megas_community/terminal_parse.py
- worker/megas/mcp/secret_policy.py -> src/megas_community/secret_policy.py

These specific modules were inspected for common credential signatures before transfer. That does not constitute a complete security, copyright or third-party licensing audit. A source-to-source review and ownership confirmation remain part of the release gate.

The CLI, trusted workspace registry, read-only MCP server, and initial test suite are newly implemented for the Community preview. They do not represent the complete internal MegasMoves operator.

Every future internal import must include internally recorded source paths/commit hashes, owner approval, dependency/license review, secret scans and platform tests. Never copy internal Git history, credentials, deployment secrets, customer data or private operations transcripts to this repository.
