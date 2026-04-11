FROM nginx:stable-alpine

# 將純 HTML/CSS/JS 檔案複製進 Nginx
# 預設會將 app/frontend 下的所有內容複製到 Nginx 的根目錄
COPY app/frontend /usr/share/nginx/html

# 開放 Nginx 預設的 80 端口
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
