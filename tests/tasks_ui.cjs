const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || path.join(root, '.venv/bin/python');
const defaultTimezone = process.env.YTDLP_DEFAULT_TIMEZONE || 'UTC';

test('server-rendered UI', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'yt-dlp-web-ui-'));
  const media = path.join(directory, 'browser-fixture.mp4');
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
        YTDLP_TEST_BARRIER_DIR: directory, YTDLP_TEST_MEDIA_FILE: media},
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
    const fixture = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
      '-i', 'testsrc2=size=160x90:rate=10', '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast',
      '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', media], {encoding: 'utf8'});
    assert.equal(fixture.status, 0, fixture.stderr);
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
    assert.equal(await form.getByLabel('TZ', {exact: true}).inputValue(), defaultTimezone);
    const apiTaskResponse = await fetch(`${url}/api/v1/tasks`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: 'https://example.test/api-default', enabled: false}),
    });
    assert.equal(apiTaskResponse.status, 201);
    const {task: apiTask} = await apiTaskResponse.json();
    assert.equal(apiTask.timezone, defaultTimezone);
    assert.equal((await fetch(`${url}/api/v1/tasks/${apiTask.id}`, {method: 'DELETE'})).status, 204);
    await form.getByLabel('Name').fill('Watcher');
    await form.getByLabel('URL').fill('https://example.test/offline');
    await form.getByRole('button', {name: 'Add'}).click();

    let task = page.locator('section').filter({has: page.getByRole('heading', {name: 'Watcher'})});
    assert.equal(await task.getByLabel('TZ', {exact: true}).inputValue(), defaultTimezone);
    await task.getByLabel('Name').fill('Edited');
    await task.getByLabel('TZ', {exact: true}).fill('Europe/London');
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
    const completedLink = page.locator('a[href^="/download/"]');
    await completedLink.waitFor();
    assert.equal(await completedLink.locator('..').locator('span').textContent(), 'Recorded - 3s');
    assert.equal(await page.locator('script').count(), 1);
    const [playback] = await Promise.all([page.waitForEvent('popup'), completedLink.click()]);
    await playback.waitForLoadState('domcontentloaded');
    assert.match(playback.url(), /\/download\//);
    await playback.waitForFunction(() => document.querySelector('video')?.readyState >= 2);
    const video = playback.locator('video');
    assert.equal(await video.evaluate(element => element.duration), 3);
    await video.evaluate(async element => { element.muted = true; await element.play(); });
    await playback.waitForFunction(() => document.querySelector('video').currentTime > 0.1);
    await video.evaluate(element => { element.pause(); element.currentTime = 1.5; });
    await playback.waitForFunction(() => {
      const element = document.querySelector('video');
      return !element.seeking && Math.abs(element.currentTime - 1.5) < 0.1;
    });
    if (process.env.YTDLP_BROWSER_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.YTDLP_BROWSER_ARTIFACT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.YTDLP_BROWSER_ARTIFACT_DIR, 'downloads.png'), fullPage: true});
      await playback.screenshot({path: path.join(process.env.YTDLP_BROWSER_ARTIFACT_DIR, 'playback.png')});
    }
    await playback.close();

    await page.getByLabel('URL').fill('https://example.test/live-second');
    await page.getByRole('button', {name: 'Download', exact: true}).click();
    await page.getByRole('button', {name: 'Stop', exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll('a[href^="/download/"]').length === 2);
    const fileLinks = await page.locator('a[href^="/download/"]').evaluateAll(links => links.map(link => link.getAttribute('href')));
    const savedFiles = new Map(fs.readdirSync(directory).filter(name => name.endsWith('.mp4'))
      .map(name => [name, fs.readFileSync(path.join(directory, name))]));

    for (const name of ['video-one', 'video-two']) {
      await page.getByLabel('URL').fill(`https://example.test/${name}`);
      await page.getByRole('button', {name: 'Download', exact: true}).click();
    }
    const firstVideo = page.locator('.job:has(a[href="https://example.test/video-one"])');
    const secondVideo = page.locator('.job:has(a[href="https://example.test/video-two"])');
    for (const width of [1000, 390]) {
      await page.setViewportSize({width, height: 800});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.getByRole('button', {name: 'Remove Controlled recording from completed list'}).count(), 2);
      assert.equal(await page.getByRole('button', {name: 'Clear all', exact: true}).isVisible(), true);
      if (process.env.YTDLP_BROWSER_ARTIFACT_DIR) {
        await page.screenshot({path: path.join(process.env.YTDLP_BROWSER_ARTIFACT_DIR, `completed-${width}.png`), fullPage: true});
      }
    }
    await page.getByRole('button', {name: 'Remove Controlled recording from completed list'}).first().click();
    assert.equal(await page.locator('a[href^="/download/"]').count(), 1);
    await page.getByRole('button', {name: 'Clear all', exact: true}).click();
    assert.equal(await page.locator('a[href^="/download/"]').count(), 0);
    assert.equal(await page.getByRole('heading', {name: 'Complete', exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: 'Clear all', exact: true}).count(), 0);
    assert.equal(await firstVideo.getByRole('button', {name: 'Stop'}).isEnabled(), true);
    assert.equal(await secondVideo.getByRole('button', {name: 'Stop'}).isEnabled(), true);
    await firstVideo.getByRole('button', {name: 'Stop'}).click();
    await page.getByRole('heading', {name: 'Stopped', exact: true}).waitFor();
    assert.equal(await firstVideo.getByRole('button').count(), 0);
    assert.equal(await secondVideo.getByRole('button', {name: 'Stop'}).isEnabled(), true);
    await secondVideo.getByRole('button', {name: 'Stop'}).click();
    await page.waitForFunction(() => !document.querySelector('form[action^="/stop/"]'));
    assert.equal(await page.getByRole('heading', {name: 'Failed', exact: true}).count(), 0);

    await stop();
    await start();
    await page.reload();
    assert.equal(await page.locator('a[href^="/download/"]').count(), 0);
    for (const [name, content] of savedFiles) {
      assert.deepEqual(fs.readFileSync(path.join(directory, name)), content);
    }
    for (const link of fileLinks) {
      const response = await fetch(`${url}${link}`);
      assert.equal(response.status, 200);
      assert.deepEqual(Buffer.from(await response.arrayBuffer()), fs.readFileSync(media));
    }
    await page.getByRole('heading', {name: 'Stopped', exact: true}).waitFor();
    assert.equal(await page.locator('.job').filter({hasText: 'Controlled video'}).count(), 2);
    await page.getByRole('link', {name: 'Tasks'}).click();
    task = page.locator('section').filter({has: page.getByRole('heading', {name: 'Edited'})});
    await task.waitFor();
    assert.equal(await task.getByLabel('Enabled').isChecked(), false);
    assert.equal(await task.getByLabel('TZ', {exact: true}).inputValue(), 'Europe/London');
    assert.equal(await form.getByLabel('TZ', {exact: true}).inputValue(), defaultTimezone);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.YTDLP_BROWSER_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.YTDLP_BROWSER_ARTIFACT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.YTDLP_BROWSER_ARTIFACT_DIR, 'lean-ui.png'), fullPage: true});
    }
    await task.getByRole('button', {name: 'Delete'}).click();
    assert.equal(await page.getByRole('heading', {name: 'Edited'}).count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    await stop();
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
