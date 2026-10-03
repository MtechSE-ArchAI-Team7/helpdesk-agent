FROM mcr.microsoft.com/playwright/mcp:latest

USER root

EXPOSE 8000

ENTRYPOINT ["node", "/app/cli.js"]

CMD ["--headless", "--browser", "chromium", "--no-sandbox", "--host", "0.0.0.0", "--allowed-hosts", "*", "--port", "8000"]