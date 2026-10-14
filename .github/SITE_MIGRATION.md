# Historical launch-planning checklist (11 October 2026)

This is pre-launch history. Current publication: [main/docs deployment](WEBSITE_DEPLOYMENT.md).

---

# Safe MegasMoves marketing-site migration

**Do not change production DNS or publish the new site to the apex domain without a route inventory and cutover test.**

The public web index on October 11, 2026 still showed older MegasMoves marketing and plans, plus public privacy/legal and installer/download references. A direct live fetch of the apex site could not be verified. These are **potentially active routes**, not automatically disposable pages.

## Existing route categories to preserve or relocate

- Marketing homepage and product/pricing presentation
- Legal/privacy/terms and policy links
- Web accounts, OAuth callbacks, session cookies, and subscription/billing pages
- Download and installer bootstrap endpoints (install.ps1, install.sh, download)
- Public mobile APK/update metadata routes
- Backend REST and WebSocket APIs, if still hosted under the same domain

## Recommended staging

1. Keep the existing megasmoves.io backend and production DNS unchanged.
2. Publish the staged static content to the GitHub Pages project URL only after owner approval.
3. Test links, responsive layout, canonical metadata and privacy/support policies.
4. Inventory production service endpoints, auth redirects and installer dependencies.
5. Separate static marketing hosting from application/API hosting using stable, verified hostnames, or retain a reverse proxy at the apex for dynamic routes. GitHub Pages alone cannot proxy the backend.
6. Update clients, callbacks and DNS only after staged compatibility tests; rollback must be possible.
7. Replace old subscription marketing claims with the current planned Megas WoW offer only after business approval.
8. Never break existing customer login, downloads, account access, legal pages, or payment cancellation paths for a cosmetic launch.

## Current limitations

The repository's site/ folder is a standalone landing-page draft and deliberately excludes any production API, authentication and billing implementation. The GitHub Actions Pages workflow is manual-only and should not be invoked until the relevant hosting and public-release decisions have been made.
