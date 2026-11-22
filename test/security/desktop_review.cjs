'use strict';
// Manual local-only audit. CDN/API responses and external browser launches are stubbed.
const { app, BrowserWindow, session, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const outputDir = process.env.SECURITY_AUDIT_DIR || '/private/tmp/stock-insight-security-audit-20261122';
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'insight-security-browser-'));
app.setPath('userData', scratch); app.setPath('sessionData', scratch);
const config = require('../../app/desktop/desktop.config.json');
const checks = [], opened = [];
const record = (id, expected, observed, severity = 'info') => checks.push({
    id, expected, observed, status: JSON.stringify(expected) === JSON.stringify(observed) ? 'pass' : 'fail', severity,
});
const b64 = data => Buffer.from(JSON.stringify(data)).toString('base64url');
const fakeToken = b64({alg:'HS256'}) + '.' + b64({sub:'00000000-0000-4000-a000-000000000001',exp:Math.floor(Date.now()/1000)+3600}) + '.audit-only';
const timeout = setTimeout(() => { console.error('Desktop security review timed out'); app.exit(1); }, 45000);
shell.openExternal = async url => { opened.push(url); };

app.whenReady().then(() => {
    const ses = session.fromPartition('persist:stock-insight');
    ses.fetch = async input => {
        const url = new URL(typeof input === 'string' ? input : input.url);
        const headers = {'Access-Control-Allow-Origin':config.frontendOrigin,
            'Access-Control-Allow-Credentials':'true', 'Access-Control-Allow-Headers':'authorization,content-type'};
        if (url.origin === new URL(config.apiBase).origin) {
            let data = {status:'success',data:[]};
            if (url.pathname.endsWith('/user/refresh')) data = {access_token:fakeToken};
            else if (url.pathname.endsWith('/user')) data = {id:'00000000-0000-4000-a000-000000000001',email:'audit@example.invalid',username:'audit',tier_name:'free'};
            return Response.json(data,{headers});
        }
        return new Response('globalThis.lucide={createIcons(){}};', {headers:{'Content-Type':'text/javascript'}});
    };
});
require('../../app/desktop/main.cjs');

async function loaded(window, url) {
    const done = new Promise(resolve => window.webContents.once('did-finish-load', resolve));
    if (url) await window.loadURL(url);
    await done;
}

app.whenReady().then(async () => {
    const main = BrowserWindow.getAllWindows()[0] || await new Promise(resolve =>
        app.once('browser-window-created', (_event, child) => resolve(child)));
    await loaded(main);
    main.hide();
    const preferences = main.webContents.getLastWebPreferences();
    record('DESKTOP-sandbox', true, preferences.sandbox);
    record('DESKTOP-isolation', true, preferences.contextIsolation);
    record('DESKTOP-node-disabled', false, preferences.nodeIntegration);
    record('DESKTOP-web-security', true, preferences.webSecurity);
    record('DESKTOP-preload-minimal', ['apiBase','login'], await main.webContents.executeJavaScript('Object.keys(stockInsightDesktop).sort()'));
    for (const link of ['file:///audit-do-not-open','http://audit.invalid','javascript:void(0)','https://user:pass@audit.invalid','https://audit.invalid']) {
        await main.webContents.executeJavaScript(`window.open(${JSON.stringify(link)},'_blank'); undefined;`);
    }
    record('DESKTOP-external-protocol-policy', ['https://audit.invalid/'], opened);
    record('DESKTOP-dot-env-denied',404,await main.webContents.executeJavaScript("fetch('/.env').then(r=>r.status)"));
    const csp = await main.webContents.executeJavaScript("fetch('/login').then(r=>r.headers.get('content-security-policy'))");
    record('DESKTOP-csp-present',true,!!csp,'medium');
    record('DESKTOP-csp-blocks-injected-script',0,await main.webContents.executeJavaScript(`(()=>{
        window.__cspMarker=0;const script=document.createElement('script');
        script.textContent='window.__cspMarker=1';document.body.appendChild(script);return window.__cspMarker;
    })()`),'high');
    await loaded(main, config.frontendOrigin + '/?sso=1');
    main.hide();
    const cases = JSON.parse(fs.readFileSync(path.join(outputDir,'xss-cases.json'),'utf8'));
    for (const test of cases) {
        const result = await main.webContents.executeJavaScript(`(async()=>{
            window.__auditMarker=0;
            const frame=document.getElementById('dr-preview-frame');
            const html=${JSON.stringify(test.html)};
            const url=URL.createObjectURL(new Blob([html],{type:'text/html'}));
            const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
            openArtifactPreview({title:'Local security fixture',htmlUrl:url,onDownload(){}});
            await loaded;
            // Cross-origin access must fail even if sanitization is bypassed.
            let parentAccess=false;
            try { parentAccess=!!frame.contentDocument; } catch {}
            await new Promise(resolve=>setTimeout(resolve,120));
            const result={marker:window.__auditMarker,sandbox:frame.getAttribute('sandbox'),
                parentAccess};
            closeArtifactPreview();URL.revokeObjectURL(url);return result;
        })()`);
        record('XSS-rendered-report-'+test.name,0,result.marker,'high');
        record('XSS-frame-isolated-'+test.name,false,result.parentAccess,'high');
    }
    // Real template code must still run under the restrictive inherited CSP.
    const deckHtml=fs.readFileSync(path.join(outputDir,'deck.html'),'utf8');
    await main.webContents.executeJavaScript(`(async()=>{
        const frame=document.getElementById('dr-preview-frame');
        const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
        openArtifactPreview({title:'Deck fixture',htmlUrl:URL.createObjectURL(new Blob([${JSON.stringify(deckHtml)}],{type:'text/html'})),onDownload(){}});
        await loaded;
    })()`);
    const deckFrame=main.webContents.mainFrame.frames.find(frame=>frame.url.startsWith('blob:'));
    const activeSlide="Array.from(document.querySelectorAll('.slide')).findIndex(slide=>slide.classList.contains('active'))";
    record('DESKTOP-deck-player-runs',0,await deckFrame.executeJavaScript(activeSlide));
    await deckFrame.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight'}))");
    record('DESKTOP-deck-next-slide',1,await deckFrame.executeJavaScript(activeSlide));
    await deckFrame.executeJavaScript("document.dispatchEvent(new KeyboardEvent('keydown',{key:'n'}))");
    record('DESKTOP-deck-notes-work',true,await deckFrame.executeJavaScript("document.getElementById('notes').classList.contains('on')"));
    await main.webContents.executeJavaScript('closeArtifactPreview()');
    // Parentless report window has no native bridge, but still shares app origin/storage.
    await main.webContents.executeJavaScript("localStorage.setItem('__security_audit_marker','fixture-only')");
    const childReady = new Promise(resolve=>app.once('browser-window-created',(_e,child)=>
        child.webContents.once('did-finish-load',()=>resolve(child))));
    await main.webContents.executeJavaScript("window.open(URL.createObjectURL(new Blob(['<p>Audit</p>'],{type:'text/html'})),'_blank','noopener');undefined;");
    const preview = await childReady;
    const state = await preview.webContents.executeJavaScript(`({node:typeof require,
        bridge:typeof stockInsightDesktop,opener:window.opener,
        appStorage:(()=>{try{return localStorage.getItem('__security_audit_marker')}catch{return null}})()})`);
    record('DESKTOP-report-native-bridge-absent','undefined',state.bridge);
    record('DESKTOP-report-node-absent','undefined',state.node);
    record('DESKTOP-report-no-opener',null,state.opener);
    record('DESKTOP-report-origin-isolated',null,state.appStorage,'high');
    record('DESKTOP-report-session-isolated',true,preview.webContents.session!==main.webContents.session,'high');
    const reportFrame=preview.webContents.mainFrame.frames[0];
    record('DESKTOP-report-frame-parent-blocked',true,await reportFrame.executeJavaScript(`(()=>{
        try { parent.document.body; return false; } catch { return true; }
    })()`),'high');
    preview.close();
    const result={scope:'Isolated Electron profile with fake API/CDN and no production connections',checks};
    fs.writeFileSync(path.join(outputDir,'desktop-runtime.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify({checks:checks.length,pass:checks.filter(c=>c.status==='pass').length,fail:checks.filter(c=>c.status==='fail').length}));
    for(const check of checks.filter(c=>c.status==='fail'))console.log(check.id,JSON.stringify(check.observed));
    clearTimeout(timeout);app.exit(checks.some(c=>c.status==='fail')?1:0);
}).catch(error=>{console.error(error);clearTimeout(timeout);app.exit(1);});
