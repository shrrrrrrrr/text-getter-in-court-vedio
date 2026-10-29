// Real browser + real API. Set PLAYWRIGHT_MODULE and optionally PLAYWRIGHT_EXECUTABLE_PATH.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
    ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
    : {}),
});
const base = process.env.COURT_BROWSER_BASE || "http://127.0.0.1:8766";
const directory = new URL("../.state/browser/", import.meta.url);
await mkdir(directory, { recursive: true });
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: "reduce",
});
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
const report = [];
const loginRecords = process.env.COURT_BROWSER_CREDENTIAL_FILE
  ? JSON.parse(await readFile(process.env.COURT_BROWSER_CREDENTIAL_FILE, 'utf8')) : null;
try {
  await page.goto(base);
  if (process.env.COURT_BROWSER_CREDENTIAL_FILE) {
    const records = loginRecords;
    await page.locator('#login-dialog').waitFor();
    await page.screenshot({path: fileURLToPath(new URL('login-desktop.png', directory)), fullPage: true});
    await page.locator('#access-credential').fill((Array.isArray(records) ? records[0] : records).credential);
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#access-credential').inputValue(), '');
    assert.equal(await page.locator('#logout-button').isVisible(), true);
    report.push('Personal credential login and input clearing passed');
  }
  await page
    .locator("#environment")
    .filter({ hasText: /本地|服务器|需要/ })
    .waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: fileURLToPath(new URL("home-desktop.png", directory)),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.locator("#open-demo").click();
  await page.locator(".segment").first().waitFor();
  const privateTaskId = await page.evaluate(() => location.hash.slice(1));
  assert.equal(await page.locator(".segment").count(), 7);
  await page.locator('[data-filter="overlap"]').click();
  assert.equal(await page.locator(".segment").count(), 2);
  await page.locator('[data-filter="all"]').click();
  await page.locator("#search").fill("我，我");
  assert.equal(await page.locator(".segment").count(), 1);
  await page.locator("#search").fill("");
  await page.locator('[data-edit="demo-2"]').click();
  await page.locator("#edit-text").fill("我是原告，嗯，我的姓名是陈某。");
  await page.locator("#edit-reviewed").check();
  await page.locator('#edit-form button[type="submit"]').click();
  await page.locator("#edit-dialog").waitFor({ state: "hidden" });
  assert.match(await page.locator("#stat-reviewed").textContent(), /^1 \/ 7$/);
  await page.locator('[data-role="SPEAKER_01"]').click();
  await page.locator("#role-name").fill("原告一");
  await page.locator("#role-status").selectOption("confirmed");
  await page.locator("#role-note").fill("合成演示：核对角色保存流程");
  await page.locator('#role-form button[type="submit"]').click();
  await page.locator("#role-dialog").waitFor({ state: "hidden" });
  assert.match(await page.locator("#speaker-list").textContent(), /原告一/);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#export-md").click(),
  ]);
  assert.equal(download.suggestedFilename(), "court-transcript.md");
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollHeight > innerHeight,
    ),
    false,
  );
  await page.screenshot({
    path: fileURLToPath(new URL("workspace-desktop.png", directory)),
    fullPage: true,
  });
  report.push(
    "1440x1000: demo, filters, search, verbatim edit, role confirmation, Markdown download, no page overflow",
  );
  for (const viewport of [
    { width: 1920, height: 1080 },
    { width: 1024, height: 768 },
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 360, height: 740 },
  ]) {
    await page.setViewportSize(viewport);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      JSON.stringify(viewport),
    );
    if (viewport.width === 390) {
      await page.screenshot({
        path: fileURLToPath(new URL("workspace-mobile.png", directory)),
        fullPage: true,
      });
      await page.locator("#mobile-menu").click();
      await page.locator("#guide-button").click();
      assert.equal(await page.locator("#guide-dialog").isVisible(), true);
      await page.locator("#guide-dialog .close-dialog").first().click();
    }
    report.push(`${viewport.width}x${viewport.height}: no horizontal overflow`);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator("#home-button").click();
  await page
    .locator("#file-input")
    .setInputFiles({
      name: "synthetic-test.wav",
      mimeType: "audio/wav",
      buffer: Buffer.alloc(20),
    });
  await page.locator("#upload-dialog").waitFor();
  assert.match(
    await page.locator("#selected-file-name").textContent(),
    /synthetic-test/,
  );
  await page.locator("#upload-dialog .close-dialog").first().click();
  if (loginRecords) {
    const audio = Buffer.alloc(44+32000);
    audio.write('RIFF',0); audio.writeUInt32LE(audio.length-8,4); audio.write('WAVEfmt ',8);
    audio.writeUInt32LE(16,16); audio.writeUInt16LE(1,20); audio.writeUInt16LE(1,22);
    audio.writeUInt32LE(16000,24); audio.writeUInt32LE(32000,28); audio.writeUInt16LE(2,32);
    audio.writeUInt16LE(16,34); audio.write('data',36); audio.writeUInt32LE(32000,40);
    await page.locator('#file-input').setInputFiles({name:'browser-control.wav',mimeType:'audio/wav',buffer:audio});
    await page.locator('#upload-dialog').waitFor();
    const uploadResponse=page.waitForResponse(response=>response.url()===base+'/api/tasks' && response.request().method()==='POST');
    await page.locator('#start-transcription').click();
    assert.equal((await uploadResponse).status(),202);
    await page.locator('#upload-dialog').waitFor({state:'hidden'});
    report.push('Real browser multipart upload with session CSRF accepted (recognition verified separately)');
  }
  if (Array.isArray(loginRecords) && loginRecords.length > 1) {
    const second = await browser.newPage();
    await second.goto(base);
    await second.locator('#login-dialog').waitFor();
    await second.locator('#access-credential').fill(loginRecords[1].credential);
    await second.locator('#login-form button').click();
    await second.locator('#login-dialog').waitFor({state:'hidden'});
    assert.equal(await second.evaluate(async () => (await fetch('/api/tasks')).json()).then(x => x.length), 0);
    assert.equal(await second.evaluate(async id => (await fetch('/api/tasks/'+id)).status, privateTaskId), 404);
    await second.close();
    await page.locator(`[data-task="${privateTaskId}"]`).click();
    await page.locator('.segment').first().waitFor();
    await page.evaluate(async () => {
      const session = await (await fetch('/api/auth/session')).json();
      await fetch('/api/auth/logout', {method:'POST',headers:{'X-CSRF-Token':session.csrf}});
    });
    await page.locator('#refresh-tasks').click();
    await page.locator('#login-dialog').waitFor();
    await page.locator('#access-credential').fill(loginRecords[1].credential);
    await page.locator('#login-form button').click();
    await page.locator('#login-dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('.segment').count(), 0);
    assert.equal(await page.evaluate(async id => (await fetch('/api/tasks/'+id)).status, privateTaskId), 404);
    await page.locator('#logout-button').click();
    await page.locator('#login-dialog').waitFor();
    assert.equal(await page.evaluate(async () => (await fetch('/api/tasks')).status), 401);
    report.push('Independent browser login, cross-user task denial, expiry/account switch clearing and logout passed');
  }
  assert.deepEqual(errors, []);
  report.push("No browser JavaScript errors");
  await writeFile(
    new URL("report.json", directory),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
}
