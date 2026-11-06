# OmniBioAI — Video Portal
# Purpose: Serve the video content directory through nginx.
# Author: Manish Kumar <manish@omnibioai.org>

FROM node:22-alpine AS frontend

WORKDIR /app
COPY package.json package-lock.json ./
COPY vendor/ ./vendor/
RUN npm ci --ignore-scripts
COPY index.html guide.html vite.config.js ./
COPY src/ ./src/
COPY public/ ./public/
COPY portal/portal.css ./portal/portal.css
RUN npm run build

FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend /app/dist/web/ /usr/share/nginx/html/
COPY content/ /usr/share/nginx/html/

EXPOSE 8086
