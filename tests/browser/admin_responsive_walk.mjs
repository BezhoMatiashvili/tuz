// Browser/layout regression checks against a disposable in-memory Python app.
// Run: node tests/browser/admin_responsive_walk.mjs
// Requires the locked Python dev environment and Playwright (PLAYWRIGHT_MODULE).
// Optional: CHROME_BIN, DASHBOARD_BROWSER=webkit, DASHBOARD_OUT, DASHBOARD_QUICK=1.
// DASHBOARD_BEFORE=1 records the baseline without failing for existing layout defects.
// No production credentials, database, Meta requests, or model calls are used.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const PORT = Number(process.env.DASHBOARD_PORT || 8792);
assert(PORT !== 8767, 'The fixture must not use the shop port');
const BASE = `http://127.0.0.1:${PORT}`;
const BEFORE = process.env.DASHBOARD_BEFORE === '1';
const QUICK = process.env.DASHBOARD_QUICK === '1';
const INTERACTIONS_ONLY = process.env.DASHBOARD_INTERACTIONS_ONLY === '1';
const OUT = process.env.DASHBOARD_OUT || path.join(tmpdir(), `tuz-dashboard-${BEFORE ? 'before' : 'after'}`);
const MODULE = process.env.PLAYWRIGHT_MODULE
  || path.join(homedir(), '.cache/mb-brag-6b61bcc/node_modules/playwright/index.mjs');
const { chromium, webkit } = await import(pathToFileURL(MODULE));
const engine = process.env.DASHBOARD_BROWSER || 'chromium';
const results = [];
const errors = [];
const accessibility = [];
const axePath = process.env.AXE_MODULE || path.resolve(path.dirname(MODULE), '../axe-core/axe.min.js');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
mkdirSync(OUT, { recursive: true });
const server = spawn(path.join(ROOT, '.venv/bin/python'),
  ['-m', 'tests.browser.admin_fixture_server', '--port', String(PORT)], { cwd: ROOT });
let serverLog = '';
server.stderr.on('data', (data) => { serverLog += data; });
server.stdout.on('data', (data) => { serverLog += data; });
let browser;
function check(name, pass, detail) {
  results.push({ name, pass: Boolean(pass), ...(detail ? { detail } : {}) });
  if (!pass) console.log(`FAIL ${name}: ${JSON.stringify(detail)}`);
}

async function settled(page) {
  await page.waitForSelector('.app[data-state="ready"]');
  await page.waitForFunction(() => !document.querySelector('main [aria-busy="true"]'));
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(60);
}

async function layout(page, name, { width, height }) {
  const found = await page.evaluate(() => {
    const main = document.querySelector('main');
    const box = main.getBoundingClientRect();
    const visible = (el) => Boolean(el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden';
    const escaped = [...main.querySelectorAll('*')].filter((el) => {
      if (!visible(el) || el.closest('.table-wrap,.subtabs,.segmented,.visually-hidden')) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && (r.right > innerWidth + 1 || r.left < -1);
    }).slice(0, 8).map((el) => `${el.tagName}.${el.className?.baseVal ?? el.className}`);
    // The phone tab bar sits outside <main>: each label must stay inside its own slot.
    const tabs = [...document.querySelectorAll('.tabbar__item')].filter(visible).filter((item) => {
      const cell = item.getBoundingClientRect();
      return [...item.children].some((child) => {
        if (child.classList.contains('nav-badge')) return false;
        const r = child.getBoundingClientRect();
        return r.left < Math.max(0, cell.left) - 0.5 || r.right > Math.min(innerWidth, cell.right) + 0.5;
      });
    }).map((item) => item.textContent.trim());
    // A sideways sub-tab strip must show the tab the page is on.
    const strip = main.querySelector('.subtabs');
    const current = strip?.querySelector('[aria-current="page"]');
    const hiddenTab = current && (() => {
      const s = strip.getBoundingClientRect();
      const r = current.getBoundingClientRect();
      return r.left < s.left - 1 || r.right > s.right + 1 ? current.textContent.trim() : '';
    })();
    // Segmented switches do not wrap: each label must fit its segment.
    const squeezed = [...main.querySelectorAll('.segmented__text')].filter(visible)
      .filter((el) => el.scrollWidth > el.clientWidth + 1).map((el) => el.textContent.trim());
    return {
      viewport: innerWidth, document: document.documentElement.scrollWidth,
      left: box.left, right: box.right, height: box.height, escaped, tabs, hiddenTab, squeezed,
      sidebar: visible(document.querySelector('.sidebar')),
      tabbar: visible(document.querySelector('.tabbar')),
    };
  });
  check(`${name}: full width`, Math.abs(found.right - found.viewport) <= 1, found);
  check(`${name}: viewport height`, found.height >= height - 1, found.height);
  check(`${name}: no page overflow`, found.document <= found.viewport + 1 && !found.escaped.length, found);
  check(`${name}: navigation`, found.sidebar === (width >= 1024) && found.tabbar === (width < 1024), found);
  check(`${name}: navigation labels fit`, !found.tabs.length, found.tabs);
  check(`${name}: current sub-tab visible`, !found.hiddenTab, found.hiddenTab);
  check(`${name}: switch labels fit`, !found.squeezed.length, found.squeezed);
}

async function session(role, lang, viewport, colorScheme) {
  const context = await browser.newContext({ viewport, colorScheme, locale: 'en-GB' });
  await context.addInitScript((language) => {
    localStorage.setItem('garage.staff.lang', language);
    window.__csp = [];
    addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));
  }, lang);
  const login = await context.request.post(`${BASE}/api/auth/login`, {
    headers: { Origin: BASE },
    data: { username: role === 'admin' ? 'boss' : 'worker', password: 'test-password-1' },
  });
  assert.equal(login.status(), 200, `Fixture login: ${await login.text()}`);
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', (error) => errors.push({ page: page.url(), message: error.message }));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) {
      errors.push({ page: page.url(), message: message.text() });
    }
  });
  page.on('response', (response) => {
    if (response.status() >= 400 && !page.expectError) {
      errors.push({ page: page.url(), status: response.status(), url: response.url() });
    }
  });
  return { context, page };
}

async function interactions(fixture, width) {
  const viewport = { width, height: width < 1024 ? 812 : 900 };
  const { context, page } = await session('admin', 'en', viewport, 'light');
  const go = async (route) => {
    await page.goto(BASE + route); await settled(page); await dismissAlarms();
  };
  const done = (name, pass = true, detail) => {
    check(`interaction/${width}: ${name}`, pass, detail);
    if (pass) console.log(`Checked interaction ${width}: ${name}`);
  };
  const action = async (method, endpoint, click) => {
    const [received] = await Promise.all([
      page.waitForResponse((r) => r.request().method() === method
        && new URL(r.url()).pathname === endpoint), click(),
    ]);
    assert(received.ok(), `${endpoint}: ${await received.text()}`);
    return received.json();
  };
  // Every full page load raises one "open alarms" toast after the first live poll (the
  // fixture has an open alarm). On a phone it sits over the bottom controls, so wait for it,
  // close it, and wait for its 220 ms exit to finish so no later click lands on a leaving toast.
  const dismissAlarms = async () => {
    const shown = page.locator('.toasts--alarm .toast--in');
    await shown.first().waitFor({ timeout: 5000 });
    while (await shown.count()) {
      const total = await page.locator('.toasts--alarm .toast').count();
      await shown.first().locator('.toast__close').click();
      await page.waitForFunction(
        (n) => document.querySelectorAll('.toasts--alarm .toast').length < n, total);
    }
  };
  const visibleControl = async (locator, name) => {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    const nav = await page.locator('.tabbar').boundingBox();
    done(name, box && box.x >= 0 && box.x + box.width <= width + 1
      && box.y >= 0 && box.y + box.height <= (nav?.y ?? viewport.height) + 1, box);
  };
  try {
    await go('/admin/inventory');
    const count = () => page.locator('.inv-card, .inv tbody tr').count();
    const initial = await count();
    await action('GET', '/api/admin/inventory', () => page.getByRole('button', { name: 'Show more' }).click());
    await page.waitForFunction((n) => document.querySelectorAll('.inv-card,.inv tbody tr').length > n, initial);
    done('inventory pagination');
    await action('GET', '/api/admin/inventory', () => page.getByRole('searchbox').fill('ZQ-1234'));
    await page.waitForTimeout(100);
    done('inventory search and URL', (await count()) > 0 && page.url().includes('q=ZQ-1234'));
    await page.setViewportSize({ width: width < 768 ? 1024 : 375, height: viewport.height });
    await page.waitForTimeout(100);
    done('inventory changes presentation on resize', await page.locator(width < 768 ? '.inv' : '.inv-cards').isVisible());
    await page.setViewportSize(viewport);
    await action('GET', '/api/admin/inventory', () => page.getByRole('searchbox').fill('doesnotexistfixture'));
    await page.getByText('Nothing matches these filters.').waitFor();
    done('inventory empty state');

    await go('/admin/content/texts/hero');
    const input = page.locator('.texts-form .input:visible').first();
    const original = await input.inputValue();
    await input.fill('სატესტო სათაური');
    await page.locator('.savebar--in').waitFor();
    await visibleControl(page.locator('.savebar').getByRole('button', { name: 'Save', exact: true }), 'save bar above navigation');
    await page.locator(`a[href="/admin/inventory"]:visible`).click();
    await page.getByRole('dialog').waitFor();
    done('unsaved-change guard');
    await page.getByRole('dialog').getByRole('button', { name: 'Stay', exact: true }).click();
    await action('PUT', '/api/admin/content/hero', () => page.locator('.savebar').getByRole('button', { name: 'Save', exact: true }).click());
    await page.waitForFunction(() => document.querySelector('.savebar').hidden);
    done('content saves');
    await input.fill(original);
    await action('PUT', '/api/admin/content/hero', () => page.locator('.savebar').getByRole('button', { name: 'Save', exact: true }).click());
    if (width < 768) {
      await page.locator('.lang-tabs').getByRole('radio', { name: 'English', exact: true }).check();
      done('mobile editor language tab', await page.locator('.pair__side[data-lang="en"]').first().isVisible());
    }

    await go('/admin/content/services/new');
    // An empty service is refused by the server (422); the form marks the fields, and on a
    // phone the language tab holding an error is named "Error in this language. English".
    page.expectError = true;
    const refused = page.waitForResponse((r) => r.request().method() === 'POST'
      && new URL(r.url()).pathname === '/api/admin/services');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    const refusal = (await refused).status();
    await page.locator('[aria-invalid="true"]').first().waitFor();
    page.expectError = false;
    done('service validation', refusal === 422
      && await page.locator('[aria-invalid="true"]').count() > 0, refusal);
    await page.locator('.pair__side[data-lang="ka"] input').first().fill('სატესტო სერვისი');
    if (width < 768) await page.locator('.lang-tabs').getByRole('radio', { name: /English$/ }).check();
    await page.locator('.pair__side[data-lang="en"] input').first().fill('Browser fixture service');
    await action('POST', '/api/admin/services', () => page.getByRole('button', { name: 'Save', exact: true }).click());
    await page.waitForURL('**/admin/content/services');
    await settled(page);
    done('service creation');
    const created = page.locator('.svc').filter({ hasText: 'Browser fixture service' });
    await created.getByRole('button', { name: /Delete/ }).click();
    await page.getByRole('dialog').waitFor();
    await visibleControl(page.getByRole('dialog').getByRole('button', { name: 'Delete', exact: true }), 'confirmation action reachable');
    await page.keyboard.press('Escape');
    done('dialog Escape restores focus', await created.getByRole('button', { name: /Delete/ }).evaluate((el) => el === document.activeElement));
    await created.getByRole('button', { name: /Delete/ }).click();
    const deletion = page.waitForResponse((r) => r.request().method() === 'DELETE' && r.url().includes('/api/admin/services/'));
    await page.getByRole('dialog').getByRole('button', { name: 'Delete', exact: true }).click();
    assert((await deletion).ok());
    done('confirmed service deletion');

    await go('/admin/content/gallery');
    const png = await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 80; canvas.height = 60;
      const ctx = canvas.getContext('2d'); ctx.fillStyle = '#386fa4'; ctx.fillRect(0, 0, 80, 60);
      return canvas.toDataURL('image/png').split(',')[1];
    });
    await action('POST', '/api/admin/media', () => page.locator('input[type="file"]').setInputFiles({
      name: 'dashboard-fixture.png', mimeType: 'image/png', buffer: Buffer.from(png, 'base64'),
    }));
    await page.locator('.photo:not(.photo--upload)').first().waitFor();
    done('gallery upload and preview', await page.locator('.photo__image').first().isVisible());
    const photo = page.locator('.photo:not(.photo--upload)').last();
    const update = page.waitForResponse((r) => r.request().method() === 'PATCH' && r.url().includes('/api/admin/media/'));
    // The fixture keeps its state between widths, so flip the switch whichever way it is.
    await photo.getByRole('switch').click();
    assert((await update).ok());
    done('gallery publish switch');

    await go(`/admin/live/${fixture.live[1]}`);
    await page.locator('.thread .bubble').last().waitFor();
    await page.locator('.thread').evaluate((el) => { el.scrollTop = 0; });
    const anchor = await page.locator('.thread .bubble').first().textContent();
    await page.locator('[data-key="older"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.thread .bubble').length === 80);
    done('older chat messages retain position', await page.locator('.thread').evaluate((el, text) => {
      const node = [...el.children].find((item) => item.textContent === text);
      return Math.abs(node.getBoundingClientRect().top - el.getBoundingClientRect().top - 12) < 3;
    }, anchor));
    const scroll = await page.locator('.thread').evaluate((el) => el.scrollTop);
    const toggle = page.locator('.live__detail').getByRole('switch');
    await toggle.focus();
    await page.waitForResponse((r) => r.url().includes('/api/admin/live/pulse'));
    done('polling retains focus and scroll', await toggle.evaluate((el) => el === document.activeElement)
      && Math.abs(await page.locator('.thread').evaluate((el) => el.scrollTop) - scroll) < 1);
    await action('PUT', `/api/admin/live/chats/${fixture.live[1]}/bot`, () => toggle.click());
    done('chat bot switch');
    await page.locator('.live__messages').scrollIntoViewIfNeeded();
    const transcript = await page.locator('.live__messages').boundingBox();
    done('transcript fits viewport', transcript.height <= viewport.height - (width < 1024 ? 60 : 0));
    if (width < 1024) {
      await page.locator('.live__back a').click();
      await page.waitForURL('**/admin/live');
      done('mobile list back link', await page.locator('.live__list').isVisible());
      await page.goBack(); await settled(page);
      done('browser back restores chat', await page.locator('.live__detail').isVisible());
    }

    await go('/admin/facebook/comments');
    await page.locator('[data-key^="reply-"]').first().click();
    await page.getByRole('dialog').waitFor();
    await page.setViewportSize({ width, height: 400 });
    const reply = page.getByRole('dialog').locator('textarea');
    await reply.fill('Fixture reply, never sent to Facebook');
    await reply.scrollIntoViewIfNeeded();
    done('keyboard-height form is scrollable', await reply.isVisible());
    if (width < 1024) {
      await page.setViewportSize(viewport);
      const simulatedViewport = async (height, offsetTop, scale, event) => page.evaluate((values) => {
        for (const key of ['height', 'offsetTop', 'scale']) {
          Object.defineProperty(window.visualViewport, key, { configurable: true, value: values[key] });
        }
        window.visualViewport.dispatchEvent(new Event(values.event));
      }, { height, offsetTop, scale, event });
      await simulatedViewport(360, 80, 1, 'resize');
      let bounds = await page.getByRole('dialog').boundingBox();
      done('keyboard visual viewport resize', bounds.y >= 80 && bounds.y + bounds.height <= 440, bounds);
      await simulatedViewport(360, 20, 1, 'scroll');
      bounds = await page.getByRole('dialog').boundingBox();
      done('keyboard visual viewport slide', bounds.y >= 20 && bounds.y + bounds.height <= 380, bounds);
      await simulatedViewport(360, 20, 2, 'resize');
      done('pinch zoom releases dialog placement', await page.getByRole('dialog').evaluate((el) => el.style.top === ''));
      await page.evaluate(() => {
        for (const key of ['height', 'offsetTop', 'scale']) delete window.visualViewport[key];
        window.visualViewport.dispatchEvent(new Event('resize'));
      });
    }
    await page.keyboard.press('Escape');
    await page.setViewportSize(viewport);

    await go('/admin/users');
    await page.getByRole('button', { name: 'Add a user', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await layout(page, `user dialog/${width}`, viewport);
    await page.keyboard.press('Escape');
    done('user dialog opens and closes');

    await go('/admin/account');
    await page.getByRole('button', { name: 'Change password', exact: true }).click();
    done('password required validation', await page.locator('[aria-invalid="true"]').count() > 0);
    await page.locator('input[autocomplete="current-password"]').fill('test-password-1');
    await page.locator('input[autocomplete="new-password"]').fill('new-test-password-2');
    await action('POST', '/api/auth/password', () => page.getByRole('button', { name: 'Change password', exact: true }).click());
    await page.locator('input[autocomplete="current-password"]').fill('new-test-password-2');
    await page.locator('input[autocomplete="new-password"]').fill('test-password-1');
    await action('POST', '/api/auth/password', () => page.getByRole('button', { name: 'Change password', exact: true }).click());
    done('password change and rotated session');
    await page.locator('.pref').getByRole('button', { name: 'Switch the interface language to Georgian' }).click();
    done('language switch', await page.locator('html').getAttribute('lang') === 'ka');

    // Error and loading states, intercepted locally and never forwarded elsewhere.
    page.expectError = true;
    await page.route('**/api/admin/overview', (route) => route.fulfill({
      status: 503, contentType: 'application/json', body: '{"detail":{"code":"UNAVAILABLE"}}',
    }));
    await go('/admin');
    done('error state', await page.locator('.state--error').isVisible());
    await page.unroute('**/api/admin/overview');
    await page.locator('.state--error button').click();
    await settled(page);
    done('retry recovers', await page.locator('.tiles').isVisible());
    page.expectError = false;
    await page.route('**/api/admin/overview', async (route) => { await delay(500); await route.continue(); });
    await page.goto(BASE + '/admin');
    await page.locator('main [aria-busy="true"]').first().waitFor();
    done('loading skeleton');
    await settled(page);
    await page.unroute('**/api/admin/overview');
    done('loading completes', await page.locator('.tiles').isVisible());
  } catch (error) {
    await page.screenshot({ path: path.join(OUT, `interaction-${width}-failure.png`) });
    writeFileSync(path.join(OUT, `interaction-${width}-failure.html`), await page.content());
    throw error;
  } finally {
    await context.close();
  }
}

try {
  let fixture;
  for (let i = 0; i < 80; i++) {
    if (server.exitCode !== null) throw new Error(`Fixture server exited: ${serverLog}`);
    try {
      fixture = await (await fetch(`${BASE}/__dashboard_fixture`)).json();
      if (fixture.fixture) break;
    } catch {}
    await delay(100);
  }
  assert(fixture?.fixture, `Fixture server failed to start: ${serverLog}`);
  browser = await (engine === 'webkit' ? webkit : chromium).launch(engine === 'webkit'
    ? { headless: true }
    : { headless: true, executablePath: process.env.CHROME_BIN || '/usr/bin/google-chrome' });
  if (!BEFORE) {
    for (const width of [375, 1440]) await interactions(fixture, width);
  }
  const common = ['/admin', '/admin/inventory', '/admin/live',
    ...fixture.live.map((key) => `/admin/live/${key}`), '/admin/account', '/admin/more'];
  const admin = ['/admin/content/texts', ...fixture.sections.map((key) => `/admin/content/texts/${key}`),
    '/admin/content/services', '/admin/content/gallery',
    ...['status', 'connect', 'chats', 'comments', 'page', 'ads'].map((tab) => `/admin/facebook/${tab}`),
    `/admin/facebook/chats/${fixture.facebook}`, '/admin/users', '/admin/audit'];
  const sizes = QUICK ? [[375, 812], [1920, 1080]]
    : [[320, 568], [375, 812], [390, 844], [768, 1024], [1023, 768], [1024, 768],
      [1440, 900], [1920, 1080], [844, 390]];
  for (const role of INTERACTIONS_ONLY ? [] : QUICK ? ['admin'] : ['admin', 'employee']) {
    for (const lang of ['ka', 'en']) {
      const { context, page } = await session(role, lang, { width: 375, height: 812 },
        lang === 'ka' ? 'dark' : 'light');
      for (const [width, height] of sizes) {
        await page.setViewportSize({ width, height });
        for (const route of [...common, ...(role === 'admin' ? admin : [])]) {
          const name = `${engine}/${role}/${lang}/${width}x${height}${route}`;
          await page.goto(BASE + route);
          await settled(page);
          await layout(page, name, { width, height });
          check(`${name}: CSP`, (await page.evaluate(() => window.__csp)).length === 0);
          if (!BEFORE && role === 'admin' && lang === 'en' && width === 375 && existsSync(axePath)) {
            await page.evaluate(readFileSync(axePath, 'utf8'));
            const audit = await page.evaluate(() => axe.run(document.querySelector('main'), {
              runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
            }));
            const violations = audit.violations.map((v) => ({ id: v.id,
              nodes: v.nodes.map((node) => node.target) }));
            accessibility.push({ route, violations });
            check(`${name}: automated accessibility`, !violations.length, violations);
          }
          if (role === 'admin' && lang === 'ka' && [375, 1920].includes(width)
            && ['/admin', '/admin/account', `/admin/live/${fixture.live[1]}`].includes(route)) {
            await page.screenshot({ path: path.join(OUT, `${width}-${route.replaceAll('/', '_')}.png`) });
          }
        }
        console.log(`Checked ${engine} ${role} ${lang} ${width}x${height}`);
      }
      await context.close();
    }
  }
} finally {
  await browser?.close();
  server.kill('SIGTERM');
  await new Promise((resolve) => server.exitCode !== null ? resolve() : server.once('exit', resolve));
  const failures = results.filter((item) => !item.pass);
  writeFileSync(path.join(OUT, 'report.json'), JSON.stringify({ engine, baseline: BEFORE,
    checks: results.length, failures, errors, accessibility, results }, null, 2));
  console.log(`${results.length} checks; ${failures.length} layout failures; ${errors.length} browser errors. ${OUT}`);
  if ((!BEFORE && failures.length) || errors.length) process.exitCode = 1;
}
