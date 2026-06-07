/**
 * 解析後端 API 基底 URL（/api 前綴）。
 *
 * 環境行為：
 * - 生產（HTTPS）：https://app.example.com/api  → ALB 將 /api/* 轉到 backend
 * - 本機 dev（HTTP）：http://localhost:8000/api  → 直連 backend 容器
 *
 * 可選覆寫（在載入本檔之前設定）：
 * - window.STOCK_INSIGHT_API_BASE = '/api' 或完整 URL
 * - window.API_BACKEND_PORT = 8000            本機 backend port
 */
function resolveStockInsightApiBase() {
    if (typeof window.STOCK_INSIGHT_API_BASE === 'string' && window.STOCK_INSIGHT_API_BASE) {
        return window.STOCK_INSIGHT_API_BASE.replace(/\/$/, '');
    }

    // 生產 HTTPS：同源 /api（方案 A — ALB path 分流，不帶 :8000）
    if (window.location.protocol === 'https:') {
        return `${window.location.origin}/api`;
    }

    // 本機 / 區網 HTTP dev：維持 hostname + backend port
    const raw = window.API_BACKEND_PORT;
    const backendPort =
        typeof raw === 'number' ? raw
        : typeof raw === 'string' ? parseInt(raw, 10) || 8000
        : 8000;
    const proto = window.location.protocol === 'https:' ? 'https:' : 'http:';
    let host = window.location.hostname;
    if (!host || host === '') {
        host = '127.0.0.1';
    }
    return `${proto}//${host}:${backendPort}/api`;
}
