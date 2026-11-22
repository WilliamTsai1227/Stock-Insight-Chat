'use strict';
const { app, BrowserWindow, ipcMain, session, shell, Menu, dialog } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const { readConfig } = require('./lib/config.cjs');
const { assetPath, CONTENT_TYPES } = require('./lib/assets.cjs');
const { createLoginAttempt } = require('./lib/login.cjs');
const { contentSecurityPolicy, reportPreviewUrl } = require('./lib/security.cjs');
const { randomUUID } = require('node:crypto');
const config = readConfig(require('./desktop.config.json'));
const rendererRoot = path.join(__dirname, 'renderer');
let mainWindow, loginAttempt;

function openExternal(url) {
    try {
        const parsed = new URL(url);
        if (parsed.protocol === 'https:' && !parsed.username && !parsed.password) {
            shell.openExternal(parsed.href).catch(() => {});
        }
    } catch { /* Reject files, custom protocols, and malformed links. */ }
}

function createWindow(desktopSession) {
    mainWindow = new BrowserWindow({
        width: 1440, height: 940, minWidth: 960, minHeight: 640,
        title: 'Stock Insight', backgroundColor: '#212121', show: false,
        icon: path.join(rendererRoot, 'icons/icon-512.png'),
        webPreferences: {
            session: desktopSession, preload: path.join(__dirname, 'preload.cjs'),
            nodeIntegration: false, contextIsolation: true, sandbox: true,
            webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
            additionalArguments: ['--insight-config=' + JSON.stringify(config)],
        },
    });
    mainWindow.once('ready-to-show', () => mainWindow.show());
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (url.startsWith('blob:' + config.frontendOrigin + '/')) {
            // The blob inherits the app origin. Read its bytes, then display it
            // inside an opaque sandbox with a separate, temporary cookie jar.
            const previewSession = session.fromPartition('report-' + randomUUID());
            previewSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
            previewSession.setPermissionCheckHandler(() => false);
            const preview = new BrowserWindow({
                parent: mainWindow, width: 1100, height: 800, title: '報告預覽',
                webPreferences: { session: previewSession, nodeIntegration: false,
                    contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false },
            });
            preview.webContents.setWindowOpenHandler(({ url: link }) => {
                openExternal(link); return { action: 'deny' };
            });
            preview.webContents.on('will-navigate', event => event.preventDefault());
            preview.webContents.on('will-redirect', event => event.preventDefault());
            mainWindow.webContents.executeJavaScript(`(async () => {
                const response = await fetch(${JSON.stringify(url)});
                const blob = await response.blob();
                if (blob.size > 10 * 1024 * 1024) throw new Error('Report too large');
                return blob.text();
            })()`).then(html => {
                if (typeof html !== 'string' || html.length > 10 * 1024 * 1024) throw new Error('Invalid report');
                return preview.loadURL(reportPreviewUrl(html));
            }).catch(() => { if (!preview.isDestroyed()) preview.close(); });
        } else {
            openExternal(url);
        }
        return { action: 'deny' };
    });
    mainWindow.webContents.on('will-attach-webview', event => event.preventDefault());
    for (const eventName of ['will-navigate', 'will-redirect']) {
        mainWindow.webContents.on(eventName, (event, url) => {
            if (new URL(url).origin !== config.frontendOrigin) {
                event.preventDefault();
                openExternal(url);
            }
        });
    }
    mainWindow.on('closed', () => {
        loginAttempt?.cancel();
        mainWindow = null;
    });
    mainWindow.loadURL(config.frontendOrigin + '/login');
}

if (!app.requestSingleInstanceLock()) {
    app.quit();
} else {
    app.on('second-instance', () => {
        if (mainWindow?.isMinimized()) mainWindow.restore();
        mainWindow?.focus();
    });
    app.whenReady().then(async () => {
        const desktopSession = session.fromPartition('persist:stock-insight');
        const deckPlayer = await fs.readFile(path.join(rendererRoot, 'js/deck-player.js'), 'utf8');
        desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
        desktopSession.setPermissionCheckHandler(() => false);
        desktopSession.on('will-download', (_event, item) => {
            item.setSaveDialogOptions({ title: '儲存檔案', defaultPath: item.getFilename() });
        });
        // Serve the bundled UI at the real public frontend origin. Cookies, CORS,
        // /login navigation, share URLs and the /explore iframe retain web semantics.
        // API and CDN requests still go to the network with normal TLS validation.
        desktopSession.protocol.handle('https', async request => {
            const url = new URL(request.url);
            const cloudRoute = /^\/(api|explore)(\/|$)/.test(url.pathname);
            if (url.origin === config.frontendOrigin && !cloudRoute) {
                const file = assetPath(request.url, config.frontendOrigin, rendererRoot);
                if (!file || !['GET', 'HEAD'].includes(request.method)) return new Response(null, { status: 404 });
                try {
                    const bytes = await fs.readFile(file);
                    return new Response(request.method === 'HEAD' ? null : bytes, { headers: {
                        'Content-Type': CONTENT_TYPES[path.extname(file)],
                        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                        'Referrer-Policy': 'strict-origin-when-cross-origin',
                        ...(path.extname(file) === '.html' ? {
                            'Content-Security-Policy': contentSecurityPolicy(bytes.toString('utf8'), config, deckPlayer),
                        } : {}),
                    } });
                } catch { return new Response(null, { status: 404 }); }
            }
            return desktopSession.fetch(request, { bypassCustomProtocolHandlers: true });
        });
        ipcMain.handle('insight:login', async event => {
            const senderUrl = new URL(event.senderFrame.url);
            if (event.sender !== mainWindow?.webContents
                || event.senderFrame !== mainWindow.webContents.mainFrame
                || senderUrl.origin !== config.frontendOrigin
                || !['/login', '/login.html'].includes(senderUrl.pathname)) {
                throw new Error('Login request rejected.');
            }
            if (loginAttempt) return { ok: false, message: '登入視窗已開啟，請先完成登入。' };
            const attempt = createLoginAttempt();
            loginAttempt = attempt;
            try {
                const redirectUri = await attempt.ready;
                const availability = await desktopSession.fetch(config.apiBase + '/public/desktop-config', {
                    signal: AbortSignal.timeout(15000), redirect: 'error',
                });
                if (!availability.ok || !(await availability.json()).enabled) {
                    throw new Error('桌面登入尚未開放，請聯絡服務管理員。');
                }
                const start = new URL(config.apiBase + '/user/auth/google/start');
                start.search = new URLSearchParams({ return_url: config.frontendOrigin,
                    desktop_redirect_uri: redirectUri, desktop_code_challenge: attempt.challenge,
                    desktop_state: attempt.state }).toString();
                await shell.openExternal(start.href);
                const code = await attempt.result;
                const response = await desktopSession.fetch(config.apiBase + '/user/auth/desktop/exchange', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    credentials: 'include', redirect: 'error', signal: AbortSignal.timeout(20000),
                    body: JSON.stringify({ code, code_verifier: attempt.verifier }),
                });
                if (!response.ok) throw new Error(response.status === 403
                    ? '此帳號目前無法使用服務。' : '登入授權已失效，請再試一次。');
                mainWindow?.show(); mainWindow?.focus();
                return { ok: true };
            } catch (error) {
                const message = /[\u4e00-\u9fff]/.test(error.message || '')
                    ? error.message : '無法連線至登入服務，請檢查網路後再試。';
                return { ok: false, message };
            } finally {
                attempt.cancel(); loginAttempt = null;
            }
        });
        Menu.setApplicationMenu(Menu.buildFromTemplate([
            { label: 'Stock Insight', submenu: [
                { label: '重新載入', role: 'reload' }, { role: 'togglefullscreen' },
                { type: 'separator' }, { label: '結束', role: 'quit' },
            ] },
            { label: '編輯', submenu: [ { role: 'undo' }, { role: 'redo' },
                { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' } ] },
            { label: '檢視', submenu: [ { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' } ] },
        ]));
        createWindow(desktopSession);
        app.on('activate', () => { if (!mainWindow) createWindow(desktopSession); });
    }).catch(error => {
        dialog.showErrorBox('Stock Insight 無法啟動', error.message);
        app.quit();
    });
    app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
    app.on('before-quit', () => loginAttempt?.cancel());
}
