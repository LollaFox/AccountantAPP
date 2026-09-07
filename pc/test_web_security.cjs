// A new headless profile and software authenticator; never the user's browser/Touch ID.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.RECEIPT_SYNC_PLAYWRIGHT || 'playwright');
const origin = process.argv[2];
assert.equal(new URL(origin).hostname, 'localhost');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.name)); // No page/credential contents in logs.
    page.on('dialog', dialog => dialog.accept());
    const cdp = await context.newCDPSession(page);
    await cdp.send('WebAuthn.enable');
    const authenticatorOptions = {
      protocol: 'ctap2', transport: 'internal', hasResidentKey: true,
      hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
    };
    let { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: authenticatorOptions });
    await page.goto(origin.replace('localhost', '127.0.0.1'));
    assert.equal(new URL(page.url()).hostname, 'localhost');
    assert.equal(await page.evaluate(() => window.isSecureContext), true);
    await page.getByRole('button', { name: '创建管理员 Passkey', exact: true }).waitFor();
    assert.equal(await page.locator('#adminContent').isVisible(), false);
    const lockedStatus = await page.evaluate(async () => (await fetch('/api/v1/devices')).status);
    assert.equal(lockedStatus, 401);
    console.log('PASS: canonical localhost, secure context, locked management');

    await page.getByRole('button', { name: '创建管理员 Passkey', exact: true }).click();
    await page.locator('#adminContent').waitFor({ state: 'visible', timeout: 15000 });
    assert.equal(await page.locator('#securityGate').isVisible(), false);
    console.log('PASS: native browser WebAuthn registration unlocks management');

    const otherTab = await context.newPage();
    await otherTab.goto(origin);
    await otherTab.getByRole('button', { name: '用 Passkey 解锁', exact: true }).waitFor();
    assert.equal(await otherTab.locator('#adminContent').isVisible(), false);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    await otherTab.close();
    console.log('PASS: a new tab has no inherited admin session or stored credential');

    await page.locator('#pairButton').click();
    await page.locator('#pairDialog').waitFor({ state: 'visible' });
    await page.locator('#deviceForm [name="name"]').fill('Disposable browser device');
    await page.locator('#deviceForm [data-mode="manual"]').click();
    await page.locator('#newDeviceCredential').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#pairToken').evaluate(element => element.textContent.length > 32), true);
    await page.locator('#pairDialog [data-close]').click();
    await page.waitForFunction(() => document.querySelector('#pairToken').textContent === '');
    assert.equal(await page.locator('#pairToken').textContent(), '');
    await page.locator('#pairButton').click();
    await page.locator('#pairDialog').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#newDeviceCredential').isVisible(), false);
    const device = page.locator('#deviceList p').filter({ hasText: 'Disposable browser device' });
    await device.locator('[data-revoke-device]').click();
    await page.waitForFunction(() => [...document.querySelectorAll('#deviceList p')].some(element =>
      element.textContent.includes('Disposable browser device') && element.textContent.includes('已撤销')));
    await page.locator('#pairDialog [data-close]').click();
    console.log('PASS: per-device creation, one-time credential display and revocation');

    await page.locator('#pairButton').click();
    await page.locator('#deviceForm [name="name"]').fill('Disposable QR invitation');
    await page.locator('#deviceForm [data-mode="qr"]').click();
    await page.locator('#pairQRSection').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#pairQR').evaluate(img => img.complete && img.naturalWidth > 0), true);
    assert.equal(await page.locator('#pairInvitation').evaluate(field => {
      const uri = new URL(field.value);
      const data = JSON.parse(atob(uri.searchParams.get('data').replace(/-/g, '+').replace(/_/g, '/')));
      return uri.protocol === 'receiptsync:' && data.version === 1 && !data.device_token
        && data.expires_at * 1000 > Date.now() && data.expires_at * 1000 <= Date.now() + 180000;
    }), true);
    const cancelled = page.waitForResponse(response => response.request().method() === 'DELETE'
      && response.url().includes('/api/v1/pairing/invitations/'));
    await page.locator('#pairDialog [data-close]').click();
    assert.equal((await cancelled).status(), 200);
    assert.equal(await page.locator('#pairInvitation').inputValue(), '');
    console.log('PASS: fresh-passkey QR invitation renders, expires in three minutes and cancels on close');

    await page.locator('#securityButton').click();
    await page.locator('#passkeyList [data-revoke-passkey]').click();
    await page.waitForFunction(() => document.querySelector('#securityMessage').textContent.includes('最后一个'));
    assert.equal(await page.locator('#adminContent').isVisible(), true);
    await page.locator('#recoveryCodeButton').click();
    await page.waitForFunction(() => document.querySelector('#recoveryCode').textContent.length > 32);
    const recoveryCode = await page.locator('#recoveryCode').textContent(); // Memory only, never logged.
    await page.locator('#securityDialog [data-close]').click();
    await page.waitForFunction(() => document.querySelector('#recoveryCode').textContent === '');
    assert.equal(await page.locator('#recoveryCode').textContent(), '');
    console.log('PASS: last-passkey deletion refused and recovery code displayed only while dialog is open');

    await page.locator('#lockButton').click();
    await page.locator('#securityGate').waitFor({ state: 'visible' });
    await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
    ({ authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: authenticatorOptions }));
    await page.locator('#securityGate summary').click();
    await page.locator('#recoveryInput').fill(recoveryCode);
    await page.locator('#recoverButton').click();
    await page.locator('#adminContent').waitFor({ state: 'visible', timeout: 15000 });
    const replay = await page.evaluate(async code => (await fetch('/api/v1/auth/recover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
    })).status, recoveryCode);
    assert.equal(replay, 400);
    console.log('PASS: lost-key recovery enrolls a replacement through WebAuthn and consumes the recovery code');

    await page.locator('#securityButton').click();
    const oldKey = page.locator('#passkeyList p').filter({ hasText: '管理员 Passkey' });
    await oldKey.locator('[data-revoke-passkey]').click();
    await page.waitForFunction(() => [...document.querySelectorAll('#passkeyList p')].some(element =>
      element.textContent.includes('管理员 Passkey') && element.textContent.includes('已撤销')));
    assert.equal(await page.locator('#adminContent').isVisible(), true);
    await page.locator('#securityDialog [data-close]').click();
    console.log('PASS: replacement passkey revokes the lost key without deleting the ledger');

    await page.locator('#lockButton').click();
    await page.locator('#securityGate').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#adminContent').isVisible(), false);
    const afterLogout = await page.evaluate(async () => (await fetch('/api/v1/summary')).status);
    assert.equal(afterLogout, 401);
    await page.getByRole('button', { name: '用 Passkey 解锁', exact: true }).click();
    await page.locator('#adminContent').waitFor({ state: 'visible', timeout: 15000 });
    console.log('PASS: logout locks access; signed browser assertion unlocks again');

    // A late pairing response must not repopulate credentials or reopen a dialog after lock.
    let requested;
    const pending = new Promise(resolve => { requested = resolve; });
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route('**/api/v1/pairing', async route => {
      const response = await route.fetch();
      requested();
      await held;
      await route.fulfill({ response });
    });
    await page.locator('#pairButton').click();
    await pending;
    await page.locator('#lockButton').click();
    release();
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('无法读取设备信息'));
    assert.equal(await page.locator('#pairDialog').isVisible(), false);
    assert.equal(await page.locator('#pairFingerprint').textContent(), '');
    assert.equal(await page.locator('#pairToken').textContent(), '');
    console.log('PASS: locking during an in-flight request does not expose pairing details');
    assert.deepEqual(errors, []);
    await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
  } finally {
    await browser.close();
  }
})().catch(error => {
  // Browser exception diagnostics can include rendered secrets; log only the failing source line.
  console.error('Browser verification failed:', error.name);
  console.error(error.stack?.split('\n').find(line => line.includes('test_web_security.cjs')) || 'No source location');
  process.exitCode = 1;
});
