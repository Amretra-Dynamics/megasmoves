# MegasMoves public-source scope

## Launch target

The official organization repository should become a useful and independently buildable **MegasMoves** open-source project, not merely a brochure or a mock interface. The current read-only Community preview is an initial testable foundation; it is **not** the complete source-release target.

The owner has authorized consideration of the MegasMoves technology outside unrelated private-product integrations. Inclusion still requires verified ownership, security, platform builds and removal of sensitive data.

## Candidate MegasMoves capabilities for public review

| Area | Internal implementation area | Publication gate |
| --- | --- | --- |
| CLI, local runtime and workspace permissions | worker/megas/cli, worker/megas/local, worker/megas/policy.py | Decouple private services; test local installation and approvals |
| Local MCP tools and protocol | worker/megas/mcp | Select publicly supportable capabilities, remove unrelated private integration mounts |
| Coding-agent adapters | worker/megas/adapters | Check supported provider terms, execution boundaries and third-party licenses |
| Browser and Windows operations | worker/megas/operate, worker/windows | Explicit permission flows, cleanup, redaction, safe focus/screenshot handling, real Windows tests |
| Linux and macOS frontends | worker/linux, worker/macos | Build and smoke-test on actual target systems |
| Android app | app | Verify permissions, manifest, signing separation, source licensing and a clean build |
| Tests and examples | worker/tests, tests, examples | Replace private fixtures, use synthetic samples only |
| Installer and downloads | reviewed platform packaging | Signed or accurately marked artifacts, checksum verification, unprivileged installation |
| Static marketing website | site | GitHub Pages and domain checks; no implied features not in a real public release |

These paths are inventory references to the private engineering source, **not evidence that their content is already exported or certified**.

## Material excluded from public distribution

- Unrelated private-product connectors, account control surfaces and product-specific endpoints.
- Customer or developer data, tokens, private workspaces, logs and incident transcripts.
- Live production infrastructure, deployment authority, API secrets, certificates and signing keys.
- Assets or dependencies not owned by Amretra Dynamics or not authorized for redistribution.
- Unreviewed modules that cannot operate safely without internal systems.

## Acceptance criteria

Each published feature must have its implementation, documentation, dependencies and tests available in the public repository. Runtime badges, device icons and marketing copy cannot claim functionality that does not pass the platform tests.

The first public release can be scoped, but it must be described accurately, genuinely usable, and independently reviewable. No public visibility or official launch tag until the owner authorizes a specific clean commit.
