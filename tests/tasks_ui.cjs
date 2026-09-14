const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || path.join(root, '.venv/bin/python');

test('server-rendered UI', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'yt-dlp-web-ui-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  let server, browser, logs = '';

  async function start() {
    server = spawn(python, ['-u', '-c',
      `from pathlib import Path; import app; app.WORKER = Path('tests/fixtures/control_worker.py').resolve(); app.init_runtime(); app.app.run(host='127.0.0.1', port=${port}, threaded=True)`], {
      cwd: root,
      env: {...process.env, YTDLP_DOWNLOAD_DIR: directory, YTDLP_STATE_DIR: path.join(directory, 'state'),
        YTDLP_TEST_BARRIER_DIR: directory},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    server.stdout.on('data', data => { logs += data; });
    server.stderr.on('data', data => { logs += data; });
    for (let attempt = 0; attempt < 100; attempt++) {
      assert.equal(server.exitCode, null, logs);
      try { if ((await fetch(`${url}/api/v1/health`)).ok) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(logs);
  }

  async function stop() {
    if (!server || server.exitCode !== null) return;
    await new Promise(resolve => { server.once('exit', resolve); server.kill('SIGTERM'); });
  }

  try {
    await start();
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage({viewport: {width: 1000, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    await page.goto(url);
    await page.getByRole('link', {name: 'Tasks'}).click();
    assert.equal(await page.locator('script').count(), 0);
    assert.deepEqual(await page.locator('nav a').allTextContents(), ['Downloads', 'Tasks']);

    let form = page.locator('section').filter({has: page.getByRole('heading', {name: 'New'})});
    await form.getByLabel('Name').fill('Watcher');
    await form.getByLabel('URL').fill('https://example.test/offline');
    await form.getByRole('button', {name: 'Add'}).click();

    let task = page.locator('section').filter({has: page.getByRole('heading', {name: 'Watcher'})});
    await task.getByLabel('Name').fill('Edited');
    await task.getByLabel('Enabled').uncheck();
    await task.getByRole('button', {name: 'Save'}).click();
    task = page.locator('section').filter({has: page.getByRole('heading', {name: 'Edited'})});
    assert.equal(await task.getByLabel('Enabled').isChecked(), false);
    await task.getByRole('button', {name: 'Run'}).click();
    await task.getByText('offline', {exact: true}).waitFor();

    await page.getByRole('link', {name: 'Downloads'}).click();
    await page.getByLabel('URL').fill('https://example.test/live');
    await page.getByRole('button', {name: 'Download'}).click();
    await page.getByRole('button', {name: 'Stop'}).waitFor();
    await page.getByRole('button', {name: 'Stop'}).click();
    fs.writeFileSync(path.join(directory, 'release-finalizing'), '');
    await page.locator('a[href^="/download/"]').waitFor();
    assert.equal(await page.locator('script').count(), 1);

    await stop();
    await start();
    await page.reload();
    await page.getByRole('link', {name: 'Tasks'}).click();
    task = page.locator('section').filter({has: page.getByRole('heading', {name: 'Edited'})});
    await task.waitFor();
    assert.equal(await task.getByLabel('Enabled').isChecked(), false);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.YTDLP_BROWSER_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.YTDLP_BROWSER_ARTIFACT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.YTDLP_BROWSER_ARTIFACT_DIR, 'lean-ui.png'), fullPage: true});
    }
    await task.getByRole('button', {name: 'Delete'}).click();
    assert.equal(await page.getByRole('heading', {name: 'Edited'}).count(), 0);
    assert.equal((await page.goto(`${url}/shortcut`)).status(), 404);
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    await stop();
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
