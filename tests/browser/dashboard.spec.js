import { test, expect } from "./fixtures.js";

async function refreshRecords(page) {
  await page.locator("#settingsButton").click();
  await page.locator("#refreshButton").click();
  await page.locator("#closeSettingsButton").click();
}

async function stopRun(page) {
  await page.locator("#runtimeActionButton[data-action=stop]").click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Stop run", exact: true }).click();
}

test("opens the default work journal and refreshes through visible controls", async ({ page, dashboard }) => {
  await expect(page).toHaveTitle(/Work journal/);
  await expect(page.locator("#currentCycle")).toContainText("Browser smoke cycle completed");
  await expect(page.locator('a[href="/legacy"]:visible')).toHaveCount(0);
  expect((await page.request.get(`${dashboard.url}/legacy`)).status()).toBe(404);
  await expect(page.locator("#runtimeActionButton")).toBeEnabled();
  await expect(page.locator("#runtimeActionButton")).toHaveAttribute("data-action", "start");
  await expect(page.locator("#runtimeActionButton")).toHaveText("Start run");
  await page.locator("#tab-logs").click();
  await expect(page.locator("#logText")).toContainText("Browser fixture runtime log");
  await expect(page.locator("#rawText")).toBeHidden();
  await page.locator("#runtimeDiagnostics .disclosure-trigger").click();
  await expect(page.locator("#rawText")).toContainText(/State=stopped|Loop: NOT RUNNING/);
  const status = page.waitForResponse((response) => response.url().endsWith("/api/journal"));
  await page.locator("#settingsButton").click();
  await page.locator("#refreshButton").click();
  await page.locator("#closeSettingsButton").click();
  expect((await status).status()).toBe(200);
  await expect(page.locator("#runtimeState")).toHaveText("Stopped");
});

test("one sidebar control and right-aligned tabs stay clear of the brand at narrow widths", async ({ page }) => {
  await expect(page.locator('#runtimeActionButton')).toHaveCount(1);
  await expect(page.locator('.topbar #runtimeControls')).toHaveCount(0);
  await expect(page.locator('#projectSidebar .project-identity #runtimeActionButton')).toBeVisible();
  await expect(page.locator('#readOnlyIndicator')).toBeHidden();
  await expect(page.locator('#journalBackLink')).toBeVisible();
  await expect(page.locator('#journalBackLink')).toHaveJSProperty('href', await page.locator('.brand').evaluate(node => node.href));
  for (const width of [1280, 1001, 1000, 760, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: `No horizontal overflow at ${width}px` }).toBe(true);
    const bounds = await page.evaluate(() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
      return { brand: rect('.brand'), tools: rect('.workspace-tools'), header: rect('.topbar'),
        navigation: rect('.navline'), tabs: rect('.ac-tabs-host'), name: rect('#projectName'),
        button: rect('#runtimeActionButton'), title: rect('.workspace-project-heading'), back: rect('#journalBackLink'),
        arrow: rect('#journalBackLink .ui-icon'),
        overflow: document.documentElement.scrollWidth > innerWidth };
    });
    expect(bounds.brand.right).toBeLessThanOrEqual(bounds.tools.left);
    expect(bounds.brand.top).toBeGreaterThanOrEqual(bounds.header.top);
    expect(bounds.name.right).toBeLessThanOrEqual(bounds.button.left);
    expect(bounds.navigation.right - bounds.tabs.right).toBeLessThanOrEqual(20);
    const inset = 8;
    expect(bounds.back.width).toBe(32);
    expect(bounds.back.height).toBe(32);
    expect(bounds.back.left - bounds.navigation.left).toBeCloseTo(inset, 1);
    expect(bounds.back.top - bounds.navigation.top).toBeCloseTo(inset, 1);
    expect(bounds.arrow.left + bounds.arrow.width / 2).toBeCloseTo(bounds.back.left + 16, 1);
    expect(bounds.arrow.top + bounds.arrow.height / 2).toBeCloseTo(bounds.back.top + 16, 1);
    expect(bounds.title.right).toBeLessThanOrEqual(bounds.tabs.left);
    expect(bounds.back.right).toBeLessThanOrEqual(bounds.title.left);
    expect(bounds.back.top + bounds.back.height / 2).toBeCloseTo(bounds.tabs.top + bounds.tabs.height / 2, 1);
    if (width > 640) expect(bounds.navigation.top).toBeLessThan(bounds.brand.bottom);
    if (width === 1280) expect(bounds.title.left + bounds.title.width / 2).toBeCloseTo(bounds.navigation.left + bounds.navigation.width / 2, 1);
    expect(bounds.overflow).toBe(false);
  }
});

test("saves shared language through HTTP and retains it after reload", async ({ page, dashboard }) => {
  await page.locator("#settingsButton").click();
  await page.locator("#languageSelect").selectOption("zh-CN");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator("#tab-work")).toHaveText("工作记录");
  const response = await page.request.get(`${dashboard.url}/api/language`);
  expect(await response.json()).toMatchObject({
    language: "zh-CN", nextLanguage: "zh-CN", source: "saved", locked: false, pending: false,
  });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await page.locator("#settingsButton").click();
  await expect(page.locator("#languageSelect")).toHaveValue("zh-CN");
});

test.describe("an active product", () => {
  test.use({ scenario: "active-product" });

  test("keeps the current language while saving the next product preference", async ({ page, dashboard }) => {
    const before = await (await page.request.get(`${dashboard.url}/api/language`)).json();
    expect(before).toMatchObject({ language: "en", nextLanguage: "en", locked: true });
    await page.locator("#settingsButton").click();
    await page.locator("#languageSelect").selectOption("zh-CN");
    await expect(page.locator("#languageHint")).toContainText(/English.*Simplified Chinese/);
    await expect(page.locator("#languageStatus")).toContainText(/next product|new product/i);
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.reload();
    await page.locator("#settingsButton").click();
    await expect(page.locator("#languageHint")).toContainText(/English.*Simplified Chinese/);
    await expect(page.locator("#languageSelect")).toHaveValue("zh-CN");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    const after = await (await page.request.get(`${dashboard.url}/api/language`)).json();
    expect(after).toMatchObject({
      language: "en", nextLanguage: "zh-CN", locked: true, pending: true, productId: before.productId,
    });
  });
});

test.describe("server errors", () => {
  test.use({ scenario: "save-failure" });

  test("shows language and action failures without claiming success", async ({ page }) => {
    await page.locator("#settingsButton").click();
    const save = page.waitForResponse((response) => response.url().endsWith("/api/language")
      && response.request().method() === "POST");
    await page.locator("#languageSelect").selectOption("zh-CN");
    expect((await save).status()).toBe(500);
    await expect(page.locator("#languageStatus")).toContainText("The language change was not confirmed.");
    await expect(page.locator("#languageSelect")).toHaveValue("en");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await page.locator("#closeSettingsButton").click();
    await page.locator("#runtimeActionButton[data-action=start]").click();
    await expect(page.locator("#actionStatus")).toContainText(/Could not confirm.*Start run/);
    await expect(page.locator("#actionStatus")).toContainText("Host actions are disabled in browser smoke tests.");
    await expect(page.locator("#runtimeActionButton[data-action=start]")).toBeEnabled();
    await expect(page.locator("#runtimeState")).toHaveText("Stopped");
  });
});

test("shows a failed journal request and recovers on refresh", async ({ page }) => {
  await page.route("**/api/journal", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ error: "Smoke test unavailable" }),
  }));
  await refreshRecords(page);
  await expect(page.locator("#connectionError")).toBeVisible();
  await expect(page.locator("#runtimeState")).toHaveText("Status unavailable");
  await expect(page.locator("#runtimeActionButton")).toBeDisabled();
  await expect(page.locator("#currentCycle")).toContainText("Browser smoke cycle completed");
  await page.unroute("**/api/journal");
  await refreshRecords(page);
  await expect(page.locator("#connectionError")).toBeHidden();
  await expect(page.locator("#runtimeState")).toHaveText("Stopped");
  await expect(page.locator("#runtimeActionButton[data-action=start]")).toBeEnabled();
});

test("a stale journal refresh cannot undo a confirmed shared-language save", async ({ page }) => {
  let release;
  let received;
  const held = new Promise((resolve) => { release = resolve; });
  const captured = new Promise((resolve) => { received = resolve; });
  await page.route("**/api/journal", async (route) => {
    const response = await route.fetch();
    received();
    await held;
    await route.fulfill({ response });
  }, { times: 1 });
  await refreshRecords(page);
  await captured;
  await page.locator("#settingsButton").click();
  await page.locator("#languageSelect").selectOption("zh-CN");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  release();
  await expect(page.locator("#refreshButton")).toBeEnabled();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator("#languageSelect")).toHaveValue("zh-CN");
});

test("unavailable bootstrap stays usable and can recover from visible controls", async ({ page }) => {
  await page.route("**/api/journal", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ error: "Unavailable" }),
  }));
  await page.reload();
  await expect(page.locator("#connectionError")).toBeVisible();
  await expect(page.locator("#refreshButton")).toBeEnabled();
  await expect(page.locator("#runtimeActionButton")).toBeDisabled();
  await page.locator("#settingsButton").click();
  await expect(page.locator("#settingsDialog")).toBeVisible();
  await page.locator("#closeSettingsButton").click();
  await page.unroute("**/api/journal");
  await refreshRecords(page);
  await expect(page.locator("#connectionError")).toBeHidden();
  await expect(page.locator("#currentCycle")).toContainText("Browser smoke cycle completed");
  await expect(page.locator("#runtimeState")).toHaveText("Stopped");
});

test.describe("host action boundary", () => {
  test.use({ scenario: "action-success" });

  test("stop is immediate during a slow refresh and disables duplicate controls", async ({ page }) => {
    await page.locator("#runtimeActionButton[data-action=start]").click();
    await expect(page.locator("#runtimeActionButton[data-action=stop]")).toBeEnabled();
    let releaseStatus, releaseStop;
    let statusSeen, stopSeen;
    const statusGate = new Promise((resolve) => { releaseStatus = resolve; });
    const stopGate = new Promise((resolve) => { releaseStop = resolve; });
    const statusReceived = new Promise((resolve) => { statusSeen = resolve; });
    const stopReceived = new Promise((resolve) => { stopSeen = resolve; });
    await page.route("**/api/journal", async (route) => {
      const response = await route.fetch();
      statusSeen();
      await statusGate;
      await route.fulfill({ response });
    }, { times: 1 });
    await page.route("**/api/action/stop", async (route) => {
      stopSeen();
      await stopGate;
      await route.continue();
    }, { times: 1 });
    await refreshRecords(page);
    await statusReceived;
    await stopRun(page);
    await stopReceived;
    await expect(page.locator("#runtimeState")).toHaveText("Stopping");
    await expect(page.locator("#runtimeActionButton")).toBeDisabled();
    releaseStatus();
    releaseStop();
    await expect(page.locator("#runtimeState")).toHaveText("Stopped");
    await expect(page.locator("#runtimeActionButton[data-action=start]")).toBeEnabled();
  });

  test("start and stop use the real HTTP protocol and refresh verified state", async ({ page, dashboard }) => {
    const start = page.waitForResponse((response) => response.url().endsWith("/api/action/start"));
    await page.locator("#runtimeActionButton[data-action=start]").click();
    expect((await start).request().method()).toBe("POST");
    expect((await start).request().postData()).toBe("{}");
    await expect(page.locator("#runtimeState")).toHaveText("Running");
    await expect(page.locator("#runtimeActionButton")).toBeEnabled();
    await expect(page.locator("#runtimeActionButton")).toHaveAttribute("data-action", "stop");
    await expect(page.locator("#runtimeActionButton")).toHaveText("Stop run");
    const running = await (await page.request.get(`${dashboard.url}/api/journal`)).json();
    expect(running).toMatchObject({ readOnly: false, runtime: { available: true, processState: "running" } });
    let stopRequests = 0;
    page.on('request', request => { if (request.url().endsWith('/api/action/stop')) stopRequests += 1; });
    await page.locator('#runtimeActionButton').click();
    await page.locator('#cancelStopConfirm').click();
    await expect(page.locator('#runtimeActionButton')).toHaveAttribute('data-action', 'stop');
    expect(stopRequests).toBe(0);
    const stop = page.waitForResponse((response) => response.url().endsWith("/api/action/stop"));
    await stopRun(page);
    expect((await stop).request().method()).toBe("POST");
    await expect(page.locator("#runtimeState")).toHaveText("Stopped");
    await expect(page.locator("#runtimeActionButton")).toBeEnabled();
    await expect(page.locator("#runtimeActionButton")).toHaveAttribute("data-action", "start");
  });
});

test.describe("a verified running cycle", () => {
  test.use({ scenario: "running-cycle" });

  test("failed stop remains visible after reload and permits retry only", async ({ page }) => {
    await stopRun(page);
    await expect(page.locator("#runtimeState")).toHaveText("Stop incomplete — retry Stop");
    await expect(page.locator("#runtimeActionButton")).toBeEnabled();
    await expect(page.locator("#runtimeActionButton")).toHaveAttribute("data-action", "stop");
    await page.reload();
    await expect(page.locator("#runtimeState")).toHaveText("Stop incomplete — retry Stop");
    await expect(page.locator("#runtimeActionButton[data-action=stop]")).toBeEnabled();
  });

  test("shows current execution separately from the last completed report", async ({ page, dashboard }) => {
    await expect(page.locator("#cycleNumber")).toHaveText("02");
    await expect(page.locator("#currentCycle")).toContainText(/running|progress/i);
    await expect(page.locator("#currentCycle")).not.toContainText("Browser smoke cycle completed");
    await expect(page.locator("#historyList")).toContainText("Browser smoke cycle completed");
    const payload = await (await page.request.get(`${dashboard.url}/api/journal`)).json();
    expect(payload.cycles[0]).toMatchObject({ id: "cycle-0002-browser", status: "running", active: true, endedAt: null });
    await page.locator("#tab-logs").click();
    await page.locator("#logSelect").selectOption("cycle-0002-browser");
    await expect(page.locator("#logText")).toContainText("Current cycle fixture log");
    await expect(page.locator("#runtimeActionButton")).toBeEnabled();
    await expect(page.locator("#runtimeActionButton")).toHaveAttribute("data-action", "stop");
  });
});
