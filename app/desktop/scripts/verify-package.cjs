'use strict';
const fs = require('node:fs');
const path = require('node:path');

(async () => {
    const { listPackage } = await import('@electron/asar');
    function find(dir) {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            const file = path.join(dir, entry.name);
            if (entry.isDirectory()) { const found = find(file); if (found) return found; }
            else if (entry.name === 'app.asar') return file;
        }
    }
    const archive = process.argv[2] || find(path.resolve(__dirname, '../dist'));
    if (!archive) throw new Error('No app.asar found; run npm run pack or npm run dist:win first.');
    const files = listPackage(archive).map(file => file.replace(/\\/g, '/'));
    const forbidden = files.filter(file => /(^|\/)\.env(?:\.|$)|(^|\/)(backend|data|deploy|venv|\.git)(\/|$)|\.(pem|key|py|sql)$/.test(file));
    if (forbidden.length) throw new Error('Unexpected server/private files in package: ' + forbidden.join(', '));
    for (const required of ['/main.cjs', '/preload.cjs', '/renderer/index.html', '/renderer/login.html']) {
        if (!files.includes(required)) throw new Error('Missing packaged asset: ' + required);
    }
    console.log(`Verified ${files.length} archive entries; no backend, .env, SQL or private-key files packaged.`);
})().catch(error => { console.error(error.message); process.exitCode = 1; });
