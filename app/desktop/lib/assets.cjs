'use strict';
const path = require('node:path');

const CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json',
    '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml',
};

function assetPath(requestUrl, origin, root) {
    const url = new URL(requestUrl);
    if (url.origin !== origin) return null;
    let pathname;
    try { pathname = decodeURIComponent(url.pathname); } catch { return null; }
    if (pathname.includes('\\') || pathname.includes('\0')) return null;
    const routes = { '/': 'index.html', '/index.html': 'index.html', '/login': 'login.html',
        '/login.html': 'login.html', '/share': 'share.html', '/share.html': 'share.html' };
    let relative = routes[pathname];
    if (!relative && /^\/share\/[^/]+$/.test(pathname)) relative = 'share.html';
    if (!relative && (/^\/(js|css|icons)\//.test(pathname) || pathname === '/manifest.json')) {
        relative = pathname.slice(1);
    }
    if (!relative || !CONTENT_TYPES[path.extname(relative)]) return null;
    const resolved = path.resolve(root, relative);
    if (!resolved.startsWith(path.resolve(root) + path.sep)) return null;
    return resolved;
}

module.exports = { assetPath, CONTENT_TYPES };
