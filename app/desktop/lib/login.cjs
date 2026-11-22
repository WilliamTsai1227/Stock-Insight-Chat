'use strict';
const http = require('node:http');
const { randomBytes, createHash, timingSafeEqual } = require('node:crypto');

function createLoginAttempt(timeoutMs = 5 * 60 * 1000) {
    const state = randomBytes(32).toString('base64url');
    const verifier = randomBytes(48).toString('base64url');
    const challenge = createHash('sha256').update(verifier).digest('base64url');
    let resolveResult, rejectResult, timer, done = false;
    const result = new Promise((resolve, reject) => { resolveResult = resolve; rejectResult = reject; });
    // The listener may fail before the caller starts awaiting the callback.
    result.catch(() => {});
    function finish(error, value) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        server.close();
        if (error) rejectResult(error); else resolveResult(value);
    }
    const server = http.createServer((req, res) => {
        if (req.headers.host !== `127.0.0.1:${server.address()?.port}`
            || !req.url.startsWith('/oauth/callback?') || req.url.length > 4096) {
            res.writeHead(400); res.end('Invalid callback.'); return;
        }
        const url = new URL(req.url, 'http://127.0.0.1');
        const received = Buffer.from(url.searchParams.get('state') || '');
        const expected = Buffer.from(state);
        if (req.method !== 'GET' || url.pathname !== '/oauth/callback'
            || received.length !== expected.length || !timingSafeEqual(received, expected)) {
            res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' });
            res.end('Invalid login callback.');
            return;
        }
        const error = url.searchParams.get('error');
        const code = url.searchParams.get('code');
        if (!error && !/^[A-Za-z0-9_-]{43}$/.test(code || '')) {
            res.writeHead(400); res.end('Invalid code.'); return;
        }
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'" });
        res.end('<!doctype html><meta charset="utf-8"><title>Stock Insight</title><p>'
            + (error ? '登入未完成，請回到 Stock Insight 重試。' : '授權完成，請回到 Stock Insight。') + '</p>');
        finish(error ? new Error('Google 登入未完成，請再試一次。') : null, code);
    });
    server.requestTimeout = 10000;
    server.headersTimeout = 10000;
    server.on('error', (error) => finish(error));
    const ready = new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', () => {
            server.removeListener('error', reject);
            timer = setTimeout(() => finish(new Error('登入已逾時，請再試一次。')), timeoutMs);
            resolve(`http://127.0.0.1:${server.address().port}/oauth/callback`);
        });
    });
    ready.catch(() => {});
    return { state, verifier, challenge, ready, result,
        cancel: () => finish(new Error('登入已取消。')) };
}

module.exports = { createLoginAttempt };
