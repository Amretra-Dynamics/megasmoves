# MegasMoves

**Your always-on SI agent for all your devices.**

MegasMoves is developed by **Amretra Dynamics**. This is the separately curated release repository, not a fork or mirror of the private development repository.

> **Release candidate, not yet public:** the current source is an **early read-only Community preview**. It is not the full Windows/Android/cloud/device-control product. Do not make this repository public until release checks are passed and the owner approves the exact commit.

## What works in this source preview

- CLI approval and revocation of explicitly trusted local folders.
- Read-only Node and Python project metadata inspection; no scripts run.
- Local MCP stdio tools: operator_status, workspace_projects, workspace_check.
- Selected bounded I/O, terminal stream parsing and sensitive env-name utility modules.
- Zero required cloud services, model APIs, telemetry, or network ports.

**Not included:** full multi-device SI agent, desktop GUI, Android controller, cloud relay, unattended control, production account connection, commercial Megas WoW functionality, signed installers or release binaries. We will not claim those features without reviewed code and end-to-end evidence.

## Install from source (Python 3.11+)

Use a clean virtual environment, then run:

~~~bash
python -m pip install -e .
megas --version
megas add .
megas projects
~~~

The add command prints a workspace ID. You can inspect that approved project, check your status, or revoke permission:

~~~bash
megas check YOUR_WORKSPACE_ID
megas status
megas remove YOUR_WORKSPACE_ID
~~~

Run the local MCP server with megas serve. A compatible MCP client can use this configuration:

~~~json
{"mcpServers":{"megasmoves-community":{"command":"megas","args":["serve"]}}}
~~~

MCP cannot register a folder, execute shell commands, run workflows, contact cloud services or read arbitrary files. It can reveal the paths of approved workspaces to the connected client, so use only a trusted client.

## Tests

~~~bash
python -m unittest discover -s tests -v
~~~

## Documentation

- [Security policy](SECURITY.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Provenance of source files](docs/PROVENANCE.md)
- [Public release checklist](docs/RELEASE_CHECKLIST.md)
- [Contributing](CONTRIBUTING.md)

## Licensing and business

This repository is **MIT licensed**. The MegasMoves and Amretra Dynamics names and logos are brand identifiers; copyright permission does not grant trademark rights.

**Megas WoW**, proposed at **$20/month**, is the separate planned commercial SI-agent service. This Community preview does not include or activate that service.
