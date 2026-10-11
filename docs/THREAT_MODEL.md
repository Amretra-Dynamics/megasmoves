# Community preview threat model

## Assets

User-approved project paths, a local trust registry and selected repository metadata. The human CLI can change trusted roots; MCP clients cannot.

## Controls

- Canonicalize a directory when it is explicitly approved by the human.
- Do not accept arbitrary file paths from MCP tools.
- Resolve and verify the approved root before inspecting it.
- Inspect only size-bounded known manifest files; do not execute them.
- Do not reveal manifest command contents.
- Avoid network calls, subprocesses and unattended device actions.

## Limitations

- The registry does not implement multi-process write locking.
- Connected local MCP clients can see approved directory paths.
- This does not sandbox malicious local processes running under the same account.
- A local account holder can modify their own trust configuration.
- This minimal source does not certify the separate full commercial product.

Do not describe this code as a remote desktop agent or complete device controller.
