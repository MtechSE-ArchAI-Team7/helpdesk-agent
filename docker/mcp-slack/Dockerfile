FROM ghcr.io/korotovsky/slack-mcp-server:v1.3.0

ENV SLACK_MCP_HOST=0.0.0.0 \
    SLACK_MCP_PORT=8000 \
    SLACK_MCP_ENABLED_TOOLS=channels_list,conversations_history,conversations_replies,users_search,conversations_add_message \
    SLACK_MCP_ADD_MESSAGE_TOOL=C0C67H6P8E6

EXPOSE 8000

ENTRYPOINT ["mcp-server"]

CMD ["--transport", "http"]