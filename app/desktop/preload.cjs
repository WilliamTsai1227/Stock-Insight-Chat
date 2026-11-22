'use strict';
const { contextBridge, ipcRenderer } = require('electron');

// All values are public. No filesystem, generic IPC, or credentials are exposed.
const config = JSON.parse(process.argv.find(arg => arg.startsWith('--insight-config=')).slice(17));
if (window.top === window && window.location.origin === config.frontendOrigin) {
    contextBridge.exposeInMainWorld('stockInsightDesktop', Object.freeze({
        apiBase: config.apiBase,
        login: () => ipcRenderer.invoke('insight:login'),
    }));
}
