'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const { assetPath } = require('../lib/assets.cjs');
const { readConfig } = require('../lib/config.cjs');
const { createLoginAttempt } = require('../lib/login.cjs');
const origin = 'https://insight-chat.xyz';
const root = path.resolve('/tmp/desktop-renderer');

test('bundled routes and share links resolve to local pages', () => {
    for (const [url, file] of [['/', 'index.html'], ['/login?error=x', 'login.html'],
        ['/share/token', 'share.html'], ['/js/auth.js', 'js/auth.js']]) {
        assert.equal(assetPath(origin + url, origin, root), path.join(root, file));
    }
});

test('assets cannot escape the renderer or expose backend/configuration files', () => {
    for (const url of ['/js/%2e%2e%2f%2e%2e%2f.env', '/js/%5c..%5c.env', '/.env',
        '/desktop.config.json', '/api/user', '/js/%00a.js', '/js/bad%ZZ.js', '/backend/app.py']) {
        assert.equal(assetPath(origin + url, origin, root), null, url);
    }
    assert.equal(assetPath('https://api.insight-chat.xyz/js/auth.js', origin, root), null);
});

test('desktop configuration rejects insecure endpoints and credentials', () => {
    for (const frontendOrigin of ['http://localhost', 'https://user:secret@example.com',
        'https://example.com/path', 'https://example.com/?key=secret']) {
        assert.throws(() => readConfig({ frontendOrigin, apiBase: 'https://api.example.com/api' }));
    }
    assert.throws(() => readConfig({ frontendOrigin: origin, apiBase: 'https://api.example.com/' }));
});

test('API override preserves existing browser behavior', () => {
    const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/js/api-config.js'), 'utf8');
    const context = vm.createContext({ window: {
        location: { protocol: 'https:', origin }, STOCK_INSIGHT_API_BASE: 'https://web.example/api',
        stockInsightDesktop: { apiBase: 'https://desktop.example/api' },
    } });
    vm.runInContext(source, context);
    assert.equal(vm.runInContext('resolveStockInsightApiBase()', context), 'https://desktop.example/api');
    delete context.window.stockInsightDesktop;
    assert.equal(vm.runInContext('resolveStockInsightApiBase()', context), 'https://web.example/api');
});

test('loopback accepts only the expected state and yields one code', async () => {
    const attempt = createLoginAttempt();
    try {
        const callback = await attempt.ready;
        assert.match(callback, /^http:\/\/127\.0\.0\.1:\d+\/oauth\/callback$/);
        assert.equal(attempt.challenge, createHash('sha256').update(attempt.verifier).digest('base64url'));
        const bad = await fetch(callback + '?state=wrong&code=' + 'a'.repeat(43));
        assert.equal(bad.status, 400);
        const malformed = await fetch(callback + '?state=' + attempt.state + '&code=bad');
        assert.equal(malformed.status, 400);
        const good = await fetch(callback + '?state=' + attempt.state + '&code=' + 'a'.repeat(43));
        assert.equal(good.status, 200);
        assert.equal(good.headers.get('referrer-policy'), 'no-referrer');
        assert.equal(await attempt.result, 'a'.repeat(43));
    } finally { attempt.cancel(); }
});

test('cancelled Google authorization returns an error and closes the listener', async () => {
    const attempt = createLoginAttempt();
    const callback = await attempt.ready;
    await fetch(callback + '?state=' + attempt.state + '&error=oauth_cancelled');
    await assert.rejects(attempt.result, /Google/);
});

test('abandoned logins time out', async () => {
    const attempt = createLoginAttempt(20);
    await attempt.ready;
    await assert.rejects(attempt.result, /逾時/);
});
