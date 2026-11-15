/**
 * 報告／簡報的共用元件：產出類型的文字說明 + 預覽視窗
 * ===================================================
 * 原本放在深度研究頁（deep-research.js）。深度研究已整合進對話，獨立頁面移除後，
 * 對話的「產生報告／簡報」（chat-artifacts.js）仍需要這兩樣，因此獨立成這個檔案。
 *
 * 預覽視窗的 iframe 不加 sandbox 是刻意的：簡報的翻頁、講稿切換都靠產出檔自己的
 * inline script，加了 sandbox 就全部失效。安全性靠後端把關 —— `templates/common.scrub_html()`
 * 已經把 LLM 產出裡的 script / iframe / on* / javascript: 全部拔掉，其餘欄位一律跳脫。
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
        newtab.addEventListener('click', () => {
            if (artifactPreviewSource && artifactPreviewSource.htmlUrl) {
                window.open(artifactPreviewSource.htmlUrl, '_blank', 'noopener');
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

    // 焦點交給 iframe，簡報才收得到方向鍵；blob: 繼承本頁 origin，拿得到 contentDocument
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
