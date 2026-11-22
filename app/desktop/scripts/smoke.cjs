'use strict';
// No Google account or cloud writes: all remote services are stubbed in this smoke check.
const { app, BrowserWindow, session, shell } = require('electron');
const { createHash } = require('node:crypto');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'stock-insight-desktop-smoke-'));
app.setPath('userData', scratch);
app.setPath('sessionData', scratch);
let server;
let loginEnabled = false, loginChallenge;
const timeout = setTimeout(() => { console.error('Electron smoke timed out'); app.exit(1); }, 30000);

app.whenReady().then(() => {
    const ses = session.fromPartition('persist:stock-insight');
    const fetch = ses.fetch.bind(ses);
    ses.fetch = (input, options) => {
        const url = new URL(typeof input === 'string' ? input : input.url);
        if (url.hostname === '127.0.0.1') return fetch(input, options);
        if (url.pathname === '/api/public/desktop-config') {
            return Promise.resolve(Response.json({ enabled: loginEnabled }));
        }
        if (url.pathname === '/api/user/auth/desktop/exchange') {
            const body = JSON.parse(options.body);
            assert.equal(body.code, 'c'.repeat(43));
            assert.equal(createHash('sha256').update(body.code_verifier).digest('base64url'), loginChallenge);
            assert.equal(options.credentials, 'include');
            return Promise.resolve(Response.json({ status: 'success' }));
        }
        if (url.pathname === '/api/user/refresh') {
            return Promise.resolve(Response.json({ detail: 'No test session' }, { status: 401 }));
        }
        return Promise.resolve(new Response('globalThis.lucide={createIcons(){}};', {
            headers: { 'Content-Type': 'text/javascript' },
        }));
    };
});

require('../main.cjs');

app.whenReady().then(async () => {
    const window = BrowserWindow.getAllWindows()[0] || await new Promise(resolve =>
        app.once('browser-window-created', (_event, child) => resolve(child)));
    assert.ok(window, 'main window created');
    const finished = new Promise(resolve => window.webContents.once('did-finish-load', resolve));
    await finished;
    window.hide();
    const state = await window.webContents.executeJavaScript(`({
        title: document.title,
        button: !!document.getElementById('google-login-btn'),
        node: typeof require,
        api: resolveStockInsightApiBase(),
        bridgeKeys: Object.keys(window.stockInsightDesktop).sort(),
    })`);
    assert.ok(state.button, 'packaged login page loaded');
    assert.equal(state.node, 'undefined');
    assert.equal(state.api, require('../desktop.config.json').apiBase);
    assert.deepEqual(state.bridgeKeys, ['apiBase', 'login']);
    assert.equal(window.webContents.getLastWebPreferences().sandbox, true);
    assert.equal(window.webContents.getLastWebPreferences().contextIsolation, true);
    const missing = await window.webContents.executeJavaScript("fetch('/.env').then(r => r.status)");
    assert.equal(missing, 404, '.env is not served');
    const notEnabled = await window.webContents.executeJavaScript('window.stockInsightDesktop.login()');
    assert.equal(notEnabled.ok, false, 'cloud opt-in failure is shown immediately');
    loginEnabled = true;
    shell.openExternal = async url => {
        const start = new URL(url);
        assert.equal(start.pathname, '/api/user/auth/google/start');
        loginChallenge = start.searchParams.get('desktop_code_challenge');
        const callback = new URL(start.searchParams.get('desktop_redirect_uri'));
        assert.equal(callback.hostname, '127.0.0.1');
        callback.search = new URLSearchParams({
            state: start.searchParams.get('desktop_state'), code: 'c'.repeat(43),
        }).toString();
        const result = await globalThis.fetch(callback);
        assert.equal(result.status, 200);
    };
    const loggedIn = await window.webContents.executeJavaScript('window.stockInsightDesktop.login()');
    assert.deepEqual(loggedIn, { ok: true }, 'browser callback and PKCE exchange complete without exposing tokens');
    window.hide();

    // Exercise Chromium's real session fetch/cookie jar against a local-only server.
    server = http.createServer((_req, response) => {
        response.setHeader('Set-Cookie', 'desktop_smoke=ok; HttpOnly; SameSite=Lax; Path=/');
        response.end('ok');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const ses = window.webContents.session;
    const response = await ses.fetch(`http://127.0.0.1:${server.address().port}/`, { credentials: 'include' });
    assert.equal(await response.text(), 'ok');
    const cookies = await ses.cookies.get({ name: 'desktop_smoke' });
    assert.equal(cookies[0].httpOnly, true, 'session fetch persists HttpOnly cookies');
    console.log("Smoke: assert.equal(cookies[0].httpOnly, true, 'session fetch persi");

    const previewReady = new Promise(resolve => app.once('browser-window-created', (_event, child) => {
        child.webContents.once('did-finish-load', () => resolve(child));
    }));
    await window.webContents.executeJavaScript(`window.open(URL.createObjectURL(
        new Blob(['<p id="preview">Report</p>'], {type:'text/html'})
    ), '_blank', 'noopener'); undefined;`);
    const preview = await previewReady;
    assert.deepEqual(await preview.webContents.executeJavaScript(`({
        node: typeof require, bridge: typeof window.stockInsightDesktop, opener: window.opener
    })`), { node: 'undefined', bridge: 'undefined', opener: null });
    const reportFrame = preview.webContents.mainFrame.frames[0];
    assert.ok(reportFrame, 'report is inside an opaque iframe');
    assert.equal(await reportFrame.executeJavaScript("document.getElementById('preview').textContent"), 'Report');
    assert.equal(await reportFrame.executeJavaScript(`(()=>{
        try { return parent.localStorage.getItem('desktop_smoke'); } catch { return 'blocked'; }
    })()`), 'blocked', 'report cannot read parent storage');
    preview.close();
    console.log('Electron smoke passed: bundled UI, sandbox, preload, API override, route isolation, cloud opt-in, browser handoff, PKCE exchange, session cookies and isolated report preview.');
    clearTimeout(timeout);
    server.close();
    app.exit(0);
}).catch(error => {
    console.error(error);
    clearTimeout(timeout);
    server?.close();
    app.exit(1);
});
