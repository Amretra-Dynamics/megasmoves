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

## Android direct-install updates

The Android preview channel uses signed APKs in public GitHub Releases. The
download page reads `/downloads.json`. Direct-install clients read
`/api/v1/mobile/latest-version` or `/mobile/releases/latest.json`.
`/v1/mobile/latest-version` and `/api/v1/update/check` carry the same metadata.
Download the APK using the versioned release URL in that metadata. GitHub Pages
serves static files and does not issue a binary redirect at `/mobile/releases/latest.apk`.

1. Test the signed universal direct-install build, including an in-place
   installation over the prior preview. Retain its APK certificate and SHA-256
   verification receipt with the build. Play-distributed apps use Play's own
   signing and update channel.
2. Create a public GitHub **prerelease** with tag `preview-YYYY-MM-DD-android-CODE`
   (use the build date). Attach `megas-moves-debug-v1.0.CODE.apk` to the draft
   before publishing it. Create a new version code for changed APK bytes.
3. The **Publish Android direct-install feed** workflow checks the published
   asset digest and byte count, verifies the APK signature with the established
   direct-install certificate, and reads the package, version and SDK levels
   from the APK. It updates the JSON catalogs in both `marketing/public/` and
   the Pages export in `docs/`, then verifies the live site. A user's Android
   installer handles confirmation. A push notification is a separate delivery
   operation.

To retry a published release, run the workflow on `main` with its existing tag.
For local promotion, use a current public main checkout with Python 3.11+,
authenticated GitHub CLI, Java and Android SDK build tools:

```sh
python3 -m unittest discover -s tools/android -p 'test_preview_feed.py'
python3 tools/android/update_preview_feed.py --tag "$RELEASE_TAG" --build-tools "$ANDROID_BUILD_TOOLS"
git diff --check
```

An optional `--apk PATH` uses verified local bytes after checking GitHub's digest.
Review and commit the generated metadata to `main`, let Pages build, then run
`python3 tools/android/update_preview_feed.py --verify-live --tag "$RELEASE_TAG"`.
The public feed accepts universal APKs up to 150 MiB (157,286,400 bytes), the
limit enforced by v1.0.548. The historical v1.0.573 asset is 159,667,244 bytes.
A v1.0.548 user installing that particular release must download its signed APK
and confirm the in-place update through the Android installer.
