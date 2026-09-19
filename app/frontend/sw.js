/**
 * Service Worker — 讓 Insight 成為可安裝的 PWA（iOS 加到主畫面後獨立視窗開啟）。
 *
 * 快取策略：
 *   - 導覽（HTML）：network-first，斷線才回退快取 → 部署新版不會拿到舊頁面。
 *   - 靜態資源（css/js/icons/manifest）：stale-while-revalidate → 秒開，背景更新。
 *   - 跨網域一律放行（CDN、api.insight-chat.xyz、/explore 代理、SSE 串流都不攔）。
 *
 * 改動前端檔案後請把 VERSION 加一，否則舊 cache 不會淘汰。
 */
const VERSION = 'v1';
const STATIC_CACHE = `insight-static-${VERSION}`;
const PAGE_CACHE = `insight-pages-${VERSION}`;
const CURRENT_CACHES = [STATIC_CACHE, PAGE_CACHE];

/** 首次安裝就抓下來的 app shell；個別檔案失敗不會讓整個安裝失敗。 */
const PRECACHE_URLS = [
    '/manifest.json',
    '/css/index.css',
    '/css/login.css',
    '/css/deep-research.css',
    '/js/api-config.js',
    '/js/pwa.js',
    '/js/auth.js',
    '/js/index.js',
    '/js/deep-research.js',
    '/js/login.js',
    '/js/legal-content.js',
    '/icons/icon-192.png',
    '/icons/apple-touch-icon.png',
];

/** 這些路徑是後端 API／反向代理，永遠不進快取。 */
const BYPASS_PREFIXES = ['/api/', '/explore/'];

const isStaticAsset = (pathname) =>
    /^\/(css|js|icons)\//.test(pathname) || pathname === '/manifest.json';

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then((cache) =>
            // allSettled：少一個檔案（例如尚未部署）也不該讓安裝整個失敗
            Promise.allSettled(PRECACHE_URLS.map((url) => cache.add(url)))
        ).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((k) => k.startsWith('insight-') && !CURRENT_CACHES.includes(k))
                    .map((k) => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('message', (event) => {
    if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

/** network-first：拿得到新的就更新快取，斷線才回退。 */
async function networkFirst(request) {
    const cache = await caches.open(PAGE_CACHE);
    try {
        const response = await fetch(request);
        if (response.ok && response.type === 'basic') {
            cache.put(request, response.clone());
        }
        return response;
    } catch (err) {
        const cached = await cache.match(request) || await cache.match('/');
        if (cached) return cached;
        throw err;
    }
}

/** stale-while-revalidate：先回快取，同時背景抓新版寫回。 */
async function staleWhileRevalidate(request) {
    const cache = await caches.open(STATIC_CACHE);
    const cached = await cache.match(request);
    const network = fetch(request)
        .then((response) => {
            if (response.ok && response.type === 'basic') {
                cache.put(request, response.clone());
            }
            return response;
        })
        .catch(() => null);
    return cached || (await network) || Promise.reject(new Error('offline'));
}

self.addEventListener('fetch', (event) => {
    const { request } = event;

    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    // 跨網域（CDN／後端 API／Google OAuth）交給瀏覽器原生處理
    if (url.origin !== self.location.origin) return;
    if (BYPASS_PREFIXES.some((p) => url.pathname.startsWith(p))) return;
    // SSE 串流不能被 Cache API 碰
    if ((request.headers.get('accept') || '').includes('text/event-stream')) return;

    if (request.mode === 'navigate') {
        event.respondWith(networkFirst(request));
        return;
    }

    if (isStaticAsset(url.pathname)) {
        event.respondWith(staleWhileRevalidate(request));
    }
});
