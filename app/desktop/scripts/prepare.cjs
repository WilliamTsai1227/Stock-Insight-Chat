'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { readConfig } = require('../lib/config.cjs');
const root = path.resolve(__dirname, '..');
const source = path.resolve(root, '../frontend');
const target = path.join(root, 'renderer');
const saved = require('../desktop.config.json');
const config = readConfig({
    frontendOrigin: process.env.DESKTOP_FRONTEND_ORIGIN || saved.frontendOrigin,
    apiBase: process.env.DESKTOP_API_BASE || saved.apiBase,
});
// Copy only frontend assets. Never walk the repo root or load a .env file.
function copy(relative) {
    const input = path.join(source, relative);
    const stat = fs.lstatSync(input);
    if (stat.isSymbolicLink()) throw new Error('Symlinks are not allowed in desktop assets: ' + relative);
    if (stat.isDirectory()) {
        fs.mkdirSync(path.join(target, relative), { recursive: true });
        for (const name of fs.readdirSync(input)) copy(path.join(relative, name));
    } else if (stat.isFile() && /\.(html|js|css|json|png|ico|svg)$/.test(relative)) {
        fs.copyFileSync(input, path.join(target, relative));
    }
}
fs.rmSync(target, { recursive: true, force: true });
fs.mkdirSync(target, { recursive: true });
for (const entry of ['index.html', 'login.html', 'share.html', 'manifest.json', 'js', 'css', 'icons']) copy(entry);
// Only the trusted slide player is shared with the backend template, never server code.
fs.copyFileSync(path.resolve(root, '../backend/deep_research/templates/deck-player.js'),
    path.join(target, 'js/deck-player.js'));
fs.writeFileSync(path.join(root, 'desktop.config.json'), JSON.stringify(config, null, 2) + '\n');
console.log(`Prepared bundled frontend → ${config.apiBase}`);
