'use strict';
const { createHash } = require('node:crypto');

function contentSecurityPolicy(html, config, deckPlayer) {
    const hashes = [];
    if (deckPlayer) hashes.push("'sha256-" + createHash('sha256').update(deckPlayer).digest('base64') + "'");
    for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
        if (match[1].trim()) hashes.push("'sha256-" + createHash('sha256').update(match[1]).digest('base64') + "'");
    }
    // Existing UI buttons use static inline handlers. Authorize their exact text.
    for (const match of html.matchAll(/\son\w+="([^"]*)"/gi)) {
        hashes.push("'sha256-" + createHash('sha256').update(match[1]).digest('base64') + "'");
    }
    return [
        "default-src 'self'", "base-uri 'none'", "object-src 'none'", "form-action 'none'",
        "frame-ancestors 'none'",
        "script-src 'self' https://cdn.jsdelivr.net https://unpkg.com https://challenges.cloudflare.com 'unsafe-hashes' " + [...new Set(hashes)].join(' '),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
        "font-src 'self' https://fonts.gstatic.com", "img-src 'self' data: blob: https:",
        `connect-src 'self' blob: ${new URL(config.apiBase).origin} https://challenges.cloudflare.com`,
        `frame-src 'self' ${new URL(config.apiBase).origin} blob: https://challenges.cloudflare.com`,
        "worker-src 'self' blob:",
    ].join('; ');
}

function reportPreviewUrl(html) {
    const escaped = html.replaceAll('&', '&amp;').replaceAll('"', '&quot;')
        .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    // Both the data: wrapper and its sandboxed srcdoc have opaque origins.
    // Inline scripts keep slide navigation working; network and app storage are blocked.
    const wrapper = '<!doctype html><meta charset="utf-8">'
        + '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        + 'frame-src about:; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">'
        + '<title>報告預覽</title><style>body{margin:0}iframe{border:0;width:100vw;height:100vh}</style>'
        + '<iframe title="報告預覽" sandbox="allow-scripts allow-popups" referrerpolicy="no-referrer" allowfullscreen srcdoc="'
        + escaped + '"></iframe>';
    return 'data:text/html;charset=utf-8,' + encodeURIComponent(wrapper);
}

module.exports = { contentSecurityPolicy, reportPreviewUrl };
