/**
 * 報告／簡報的共用元件：產出類型的文字說明 + 預覽視窗
 * ===================================================
 * 原本放在深度研究頁（deep-research.js）。深度研究已整合進對話，獨立頁面移除後，
 * 對話的「產生報告／簡報」（chat-artifacts.js）仍需要這兩樣，因此獨立成這個檔案。
 *
 * iframe 只允許簡報腳本和開啟連結，不允許同源權限；報告不能讀取主頁的憑證或 DOM。
 * 後端另以 HTML parser 清理模型內容，兩層防護各自生效。
 */

const DR_SKILL_META = {
    report: {
        label: '研究報告',
        icon: 'file-text',
        desc: '結構化長文：摘要、關鍵數字、分節論述與來源清單。'
            + '產出 Word 檔（可直接編輯）與 HTML 檔（可預覽、列印成 PDF）。',
    },
    deck: {
        label: '簡報',
        icon: 'presentation',
        desc: '一頁一個論點，附講稿。產出 PowerPoint 檔（講稿在備忘稿裡）'
            + '與 HTML 檔（瀏覽器直接放映，按 N 顯示講稿）。',
    },
};

/** @type {null | {title: string, htmlUrl: string, primaryLabel: string, onDownload: () => void}} */
let artifactPreviewSource = null;

const apEl = (id) => document.getElementById(id);

function initArtifactPreviewModal() {
    const modal = apEl('dr-preview-modal');
    if (!modal || modal.dataset.bound) return;
    modal.dataset.bound = '1';

    const close = apEl('dr-preview-close');
    if (close) close.addEventListener('click', closeArtifactPreview);

    // 點背景關閉；點到卡片內部不關
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeArtifactPreview();
    });

    const download = apEl('dr-preview-download');
    if (download) {
        download.addEventListener('click', () => {
            if (artifactPreviewSource) artifactPreviewSource.onDownload();
        });
    }

    const newtab = apEl('dr-preview-newtab');
    if (newtab) {
        newtab.addEventListener('click', async () => {
            if (artifactPreviewSource && artifactPreviewSource.htmlUrl) {
                const url = artifactPreviewSource.htmlUrl;
                // Reserve the tab during the click so browsers do not block it.
                // Electron creates its own opaque report wrapper in the main process.
                if (window.stockInsightDesktop) {
                    window.open(url, '_blank', 'noopener');
                    return;
                }
                const tab = window.open('', '_blank');
                if (!tab) return;
                tab.opener = null;
                try {
                    const html = await (await fetch(url)).text();
                    const frame = tab.document.createElement('iframe');
                    frame.title = '產出預覽';
                    frame.setAttribute('sandbox', 'allow-scripts allow-popups');
                    frame.referrerPolicy = 'no-referrer';
                    frame.allowFullscreen = true;
                    frame.style.cssText = 'border:0;width:100vw;height:100vh';
                    tab.document.body.style.margin = '0';
                    frame.srcdoc = html;
                    tab.document.body.replaceChildren(frame);
                } catch {
                    tab.close();
                }
            }
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && artifactPreviewSource) closeArtifactPreview();
    });
}

/**
 * 在預覽視窗開一份 HTML 產出。
 * @param {{title: string, htmlUrl: string, primaryLabel: string, onDownload: () => void}} source
 */
function openArtifactPreview(source) {
    initArtifactPreviewModal();
    const modal = apEl('dr-preview-modal');
    const frame = apEl('dr-preview-frame');
    if (!modal || !frame || !source || !source.htmlUrl) return;

    artifactPreviewSource = source;
    apEl('dr-preview-title').textContent = source.title || '預覽';

    // 下載鍵給的是主要格式（Word / PowerPoint）：看完 HTML 後要帶走的是能編輯的那份
    const downloadBtn = apEl('dr-preview-download');
    const span = downloadBtn && downloadBtn.querySelector('span');
    if (span) span.textContent = source.primaryLabel ? `下載 ${source.primaryLabel}` : '下載';

    frame.src = source.htmlUrl;
    modal.classList.add('show');
    document.body.style.overflow = 'hidden';

    // 焦點交給 iframe；sandbox 隔離同源權限，跨來源存取會被瀏覽器拒絕。
    frame.addEventListener(
        'load',
        () => {
            try {
                frame.contentWindow.focus();
                frame.contentDocument.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') closeArtifactPreview();
                });
            } catch {
                /* 取不到就算了，右上角還有關閉鈕 */
            }
        },
        { once: true }
    );

    if (window.lucide) lucide.createIcons({ el: modal });
}

function closeArtifactPreview() {
    const modal = apEl('dr-preview-modal');
    const frame = apEl('dr-preview-frame');
    if (!modal) return;
    modal.classList.remove('show');
    document.body.style.overflow = '';
    artifactPreviewSource = null;
    // 清掉 src，否則簡報的 rAF／事件監聽會在關閉後繼續跑
    if (frame) frame.removeAttribute('src');
}

document.addEventListener('DOMContentLoaded', initArtifactPreviewModal);
