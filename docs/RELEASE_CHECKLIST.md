# Public release gate

Repository: Amretra-Dynamics/megasmoves
Candidate branch: release/community-preview
License: MIT
Status: **NOT APPROVED FOR PUBLICATION**

## Scope and hygiene
- [ ] Confirm this limited Community preview is sufficient for the desired public launch, or add more reviewed product functionality
- [ ] Verify copyright and third-party code licenses, including the three sourced modules
- [ ] Scan the entire branch content **and history** for credentials, PII and internal infrastructure
- [ ] Independently review the final feature claims and installation guide

## Security and platform tests
- [ ] Perform manual source review of trust boundaries, race conditions and error surfaces
- [ ] Run secret scanning with a dedicated scanner on the public candidate
- [ ] Run Linux and Windows CI; record successful run IDs and exact SHA
- [ ] Test installation and MCP discovery on clean developer machines
- [ ] Audit dependencies and resolve/scope historical security findings
- [ ] Verify executable packaging if binaries are promised

## Launch
- [ ] Verify site privacy/support statements, domain configuration, and all links
- [ ] Obtain founder sign-off for the exact release commit
- [ ] Change **this organization repository only** from private to public
- [ ] Confirm outside users can access GitHub, install instructions and artifacts
- [ ] Publish YouTube and X announcements with working links
- [ ] Monitor security and installation issues

The existence of this checklist does not mean it is complete. Do not automatically deploy GitHub Pages or change visibility.
