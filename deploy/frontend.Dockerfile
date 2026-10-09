FROM nginx:stable-alpine

# nginx 官方映像的這支 entrypoint script 會跑 `apk manifest nginx`（需要連 Alpine
# repo）來判斷 default.conf 是否為原廠檔，好自動補上 IPv6 listen。我們用的是自訂
# 設定，checksum 本來就不會相符 → 對我們是 no-op，但網路不通時它會卡住不放，
# 導致容器狀態是 Up 卻沒有任何 listener。直接移除，讓啟動不依賴外部網路。
RUN rm -f /docker-entrypoint.d/10-listen-on-ipv6-by-default.sh

# 使用自訂 Nginx 設定
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf

# 將純 HTML/CSS/JS 檔案複製進 Nginx
# 目標: 讓頁面可用 /login.html 與 /index.html（不需 /html/）
COPY app/frontend/css /usr/share/nginx/html/css
COPY app/frontend/js /usr/share/nginx/html/js
COPY app/frontend/*.html /usr/share/nginx/html/
# PWA：manifest／service worker／圖示都必須位於站台根目錄（sw.js 的 scope 是 /）
COPY app/frontend/icons /usr/share/nginx/html/icons
COPY app/frontend/manifest.json /usr/share/nginx/html/
COPY app/frontend/sw.js /usr/share/nginx/html/

# 開放 Nginx 預設的 80 端口
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
