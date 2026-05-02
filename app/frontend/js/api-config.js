/**
 * 解析後端 API 基底 URL（/api 前綴）。
 * 使用「目前網頁的 hostname + 固定後端埠」，區網用 http://192.168.x.x 開頁時會自動打同一台機器的 :8000。
 *
 * 可選：在任一頁面載入本檔之前設定 window.API_BACKEND_PORT（數字或字串）。
 */
function resolveStockInsightApiBase() {
    const raw = window.API_BACKEND_PORT;
    const backendPort =
        typeof raw === 'number' ? raw
        : typeof raw === 'string' ? parseInt(raw, 10) || 8000
        : 8000;
    const proto =
        window.location.protocol === 'https:' ? 'https:' : 'http:';
    let host = window.location.hostname;
    if (!host || host === '') {
        host = '127.0.0.1';
    }
    return `${proto}//${host}:${backendPort}/api`;
}
