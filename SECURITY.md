# Security Policy

This is a pre-release Community candidate. No production-security support SLA is offered. Report possible vulnerabilities privately to repository maintainers via GitHub's private vulnerability reporting if enabled, rather than posting active exploits or credentials in public issues.

## Security boundaries

- The human CLI alone can approve or revoke local workspace roots.
- MCP exposes read-only typed tools, not arbitrary shell, network operations, state changes, process starts, or file reads.
- Workspaces are identified by stable IDs and restricted to approved canonical paths.
- The service rejects symlink retargeting and bounds the size of parsed manifests.
- Manifest values are never used to execute scripts or subprocesses.
- The local client can see approved absolute paths. Connect only trusted MCP clients.
- The local trust configuration uses restrictive permissions on Unix; Windows follows its OS account protections.
- No remote server, telemetry, API key or cloud service is required for this preview.

## Publication blockers

Before releasing, scan the **complete public commit history** and all source for credentials, proprietary data, internal endpoints, and dependency issues. Obtain approval for the copyright status and any third-party code. Review scope against previous internal audit findings rather than assuming they were fixed.
