import { expect, test } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";

let server: ChildProcess;
let urls: Record<string, string>;
test.beforeAll(async () => {
  server = spawn("python", ["../tools/marketing_preview_fixture.py"], { stdio: ["ignore", "pipe", "pipe"] });
  const [chunk] = await once(server.stdout!, "data");
  urls = JSON.parse(chunk.toString());
});
test.afterAll(() => { server?.kill(); });

test("desktop platform downloads, ISO dates and copy state", async ({ page }, info) => {
  // Test the page-owned copy handler without reading or changing the human's
  // operating-system clipboard.
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
    configurable: true, value: { writeText: async () => {} },
  }));
  await page.goto(`${urls.normal}/download/`);
  const cards = page.locator(".platform-download");
  await expect(cards).toHaveCount(4);
  for (const name of ["Windows", "Android", "macOS", "Linux"]) {
    const card = cards.filter({ has: page.getByRole("heading", { name, exact: true }) });
    const href = await card.getByRole("link", { name: "Download", exact: true }).getAttribute("href");
    expect(href).toMatch(/^https:\/\/github\.com\/Amretra-Dynamics\/megasmoves\/releases\/download\//);
  }
  await expect(cards.first()).toContainText("14 Oct 2026");
  await expect(page.getByText("Invalid Date", { exact: true })).toHaveCount(0);
  await page.getByLabel("Choose your device").selectOption("Windows");
  await page.getByRole("button", { name: "Copy Windows command" }).click();
  await expect(page.getByRole("button", { name: "Copy Windows command" })).toContainText("Copied");
  await expect(page.locator("#terminal-install")).toContainText("irm https://megasmoves.io/install.ps1 | iex");
  await expect(page.locator("#terminal-install")).not.toContainText("127.0.0.1");
  await page.evaluate(() => { scrollTo({ top: 0, left: 0, behavior: "instant" }); });
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.evaluate(() => document.fonts.ready);
  const visual = page.locator(".download-hero__visual");
  const geometry = await visual.boundingBox();
  expect(geometry!.height).toBeGreaterThan(200);
  expect(await visual.locator("img").evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  await page.screenshot({ path: info.outputPath("desktop-downloads.png"), animations: "disabled" });
  await page.locator(".platform-download-grid").screenshot({ path: info.outputPath("platform-downloads.png"), animations: "disabled" });
});

test("mobile layout and denied clipboard have a usable fallback", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
    configurable: true, value: { writeText: async () => { throw new Error("Clipboard unavailable"); } },
  }));
  await page.goto(`${urls.normal}/download/`);
  await expect(page.locator(".platform-download a.button")).toHaveCount(4);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Copy website package command" }).click();
  await expect(page.locator("#terminal-install [role=alert]")).toContainText("Select the command above to copy it");
  await page.evaluate(() => { scrollTo({ top: 0, left: 0, behavior: "instant" }); });
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: info.outputPath("mobile-downloads.png"), animations: "disabled" });
});

test("failed catalogs explain the problem instead of loading forever", async ({ page }) => {
  await page.goto(`${urls.unavailable}/download/`);
  await expect(page.getByText("Downloads could not be loaded. Please refresh the page.")).toBeVisible();
  await expect(page.getByText(/The Python download could not be loaded/)).toBeVisible();
  await expect(page.getByText("Loading downloads…", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Loading the Python download…", { exact: true })).toHaveCount(0);
  await expect(page.locator(".platform-download a.button")).toHaveCount(0);
});

test("mismatched asset filename never becomes a download button", async ({ page }) => {
  await page.goto(`${urls.invalid}/download/`);
  await expect(page.getByText("Downloads could not be loaded. Please refresh the page.")).toBeVisible();
  await expect(page.locator(".platform-download a.button")).toHaveCount(0);
});
