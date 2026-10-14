# Website deployment

The production website source is `marketing/`. It is a static export of the
MegasMoves marketing frontend, using the existing cream/teal design and bundled
fonts. Public hosting is GitHub Pages at https://megasmoves.io.

Build locally (Node 22 or later):

```text
cd marketing
npm ci --no-audit --no-fund
npm run build
cd ..
python tools/check_marketing_export.py
```

Commit and push the reviewed source to `main`, then publish the checked output:

```text
python tools/publish_pages_local.py --publish
```

Pages serves the root of `gh-pages`. Its `.nojekyll` file preserves the prebuilt
Next.js output without a hosted Node build. The publishing tool preserves branch
history and rejects dirty source. It uses the existing Git login and stores no
credentials. `site-version.json` identifies the deployed source commit.

Large native downloads live in GitHub Releases. The website catalog pins their
exact release URLs, versions, sizes and SHA-256 checksums. The small Python wheel
is served directly from the custom domain. Preview artifacts remain labelled as
preview; publishing the website does not certify a signed production app release.

After deployment, check the homepage, downloads, docs and legal routes, both
installer scripts, the catalog, the Python wheel and all native asset URLs. Keep
the existing custom domain and HTTPS setting. No DNS or mail changes are needed.
