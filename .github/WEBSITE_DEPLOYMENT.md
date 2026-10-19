# Website deployment

MegasMoves source lives in `marketing/` on `main`. The static Next.js
export is published from **main /docs** at https://megasmoves.io.
Only the generated public HTML/assets, `.nojekyll`, `CNAME`, install
scripts, release catalogs and small Python wheel may go in `docs/`.
This does NOT use a gh-pages branch, GitHub Actions Pages artifact,
new repository, or hosted Node build.
Local validation is required before publication; a hosted build is not required
to produce the static export.

From a clean, up-to-date main checkout (Node 22 and Python):

```text
cd marketing
npm ci --no-audit --no-fund
npm run build
npm test
cd ..
python tools/check_marketing_export.py
python tools/publish_pages_local.py --publish
```

Browser checks use an isolated headless Chromium profile. Set
`MEGAS_TEST_CHROMIUM` to an existing trusted Chromium executable, or install
the test browser once with `npx playwright install chromium` before `npm test`.
The tests exercise desktop/mobile downloads, rejected metadata, failed fetches
and clipboard-denied feedback without accessing the operating-system clipboard.

The publisher validates links, routes, release URL pins, wheel SHA-256
and package size, and replaces only the static website files in docs/.
After pushing main, Pages deploys docs/. site-version.json records the
website's source commit. Preserve the existing cream/teal product UI,
custom domain and HTTPS, install.ps1, install.sh, /download/, /docs/,
legal routes, downloads.json and wheel. Large native binaries remain in
GitHub Releases. Never publish private auth, API, billing or runtime data.

After deployment verify the homepage, downloads, docs, pricing, legal
pages, scripts, wheel, catalog and referenced native release URLs.
