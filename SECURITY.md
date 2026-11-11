# Security Policy

This is a pre-release Community candidate. No production-security support SLA is offered. Report possible vulnerabilities privately to repository maintainers via GitHub's private vulnerability reporting if enabled, rather than posting active exploits or credentials in public issues.

## Security boundaries

- The human CLI alone can approve or revoke local workspace roots.
- The workspace catalog is read-only. Actions and Tasks have typed local write operations. The installed native operator enforces its device permissions for native operations.
- Custom MCP servers are registered by the owner. Review each tool grant and any credential binding. A stdio plugin runs with the OS account of the local operator. A remote bearer is bound to its approved origin.
- Scheduled work uses a signed-in owner and an exact skill revision. Sensitive actions retain the approval requirements of the executing host.
- Workspaces are identified by stable IDs and restricted to approved canonical paths.
- The service rejects symlink retargeting and bounds the size of parsed manifests.
- Manifest values are never used to execute scripts or subprocesses.
- The local client can see approved absolute paths. Connect only trusted MCP clients.
- The local trust configuration uses restrictive permissions on Unix; Windows follows its OS account protections.
- No remote server, telemetry, API key or cloud service is required for this preview.

## Publication blockers

Before releasing, scan the **complete public commit history** and all source for credentials, proprietary data, internal endpoints, and dependency issues. Obtain approval for the copyright status and any third-party code. Review scope against previous internal audit findings rather than assuming they were fixed.
