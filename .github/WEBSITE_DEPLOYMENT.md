# Website deployment

MegasMoves source lives in `marketing/` on `main`. The static Next.js
export is published from **main /docs** at https://megasmoves.io.
Only the generated public HTML/assets, `.nojekyll`, `CNAME`, install
scripts, release catalogs and small Python wheel may go in `docs/`.
This does NOT use a gh-pages branch, GitHub Actions Pages artifact,
new repository, or hosted Node build.

From a clean, up-to-date main checkout (Node 22 and Python):

```text
cd marketing
npm ci --no-audit --no-fund
npm run build
cd ..
python tools/check_marketing_export.py
python tools/publish_pages_local.py --publish
```

The publisher validates links, routes, release URL pins, wheel SHA-256
and package size, and replaces only the static website files in docs/.
After pushing main, Pages deploys docs/. site-version.json records the
website's source commit. Preserve the existing cream/teal product UI,
custom domain and HTTPS, install.ps1, install.sh, /download/, /docs/,
legal routes, downloads.json and wheel. Large native binaries remain in
GitHub Releases. Never publish private auth, API, billing or runtime data.

After deployment verify the homepage, downloads, docs, pricing, legal
pages, scripts, wheel, catalog and referenced native release URLs.
