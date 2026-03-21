# Rastir Config UI
FROM nginx:alpine

# Copy static files
COPY public/ /usr/share/nginx/html/
COPY src/app.js /usr/share/nginx/html/app.js

# nginx config template — API_URL is substituted at container start
COPY nginx.conf /etc/nginx/templates/default.conf.template

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
