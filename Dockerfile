FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY content/ /usr/share/nginx/html/

EXPOSE 8086
