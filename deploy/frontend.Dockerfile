FROM nginx:stable-alpine

# 使用自訂 Nginx 設定
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf

# 將純 HTML/CSS/JS 檔案複製進 Nginx
# 目標: 讓頁面可用 /login.html 與 /index.html（不需 /html/）
COPY app/frontend/css /usr/share/nginx/html/css
COPY app/frontend/js /usr/share/nginx/html/js
COPY app/frontend/html/*.html /usr/share/nginx/html/

# 開放 Nginx 預設的 80 端口
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
