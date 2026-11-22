'use strict';

function readConfig(raw) {
    const frontend = new URL(raw.frontendOrigin);
    const api = new URL(raw.apiBase);
    for (const url of [frontend, api]) {
        if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
            throw new Error('Desktop endpoints must be public HTTPS URLs without credentials or query strings.');
        }
    }
    if (frontend.pathname !== '/' || api.pathname.replace(/\/$/, '') !== '/api') {
        throw new Error('Expected a frontend origin and an API URL ending in /api.');
    }
    return Object.freeze({ frontendOrigin: frontend.origin, apiBase: api.href.replace(/\/$/, '') });
}

module.exports = { readConfig };
