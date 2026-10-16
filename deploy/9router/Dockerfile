# 9Router 0.5.99 (MIT, https://github.com/decolua/9router). Configure ONLY licensed provider API keys via its dashboard;
# do not attach subscription/OAuth accounts of other people or bypass rate limits. Private network only.
FROM node:24.22.0-trixie-slim
RUN npm install -g --no-audit --no-fund 9router@0.5.99 && useradd --uid 10002 --create-home router
USER 10002
ENV PORT=20128
EXPOSE 20128
CMD ["9router", "--host", "0.0.0.0", "--port", "20128", "--no-browser", "--skip-update"]
