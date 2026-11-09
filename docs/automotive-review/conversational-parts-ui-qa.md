# Conversational parts assistant: UI verification

Validated the conversation UI with the existing local static server and headless Chromium.
Conversation responses were synthetic and intercepted in the browser; this does not claim live
Anthropic, Motorium, or database verification.

## Checks completed

- Desktop viewport: 1440 × 1000; mobile viewport: 390 × 844.
- Chat is the primary workspace; direct searches remain in a collapsed native details element.
- Ctrl+Enter submits; New chat clears the selected vehicle and transcript, issues DELETE,
  and returns focus to the message composer.
- Catalog cards identify their manufacturer, main and matched codes, match source,
  fitment uncertainty, unknown price, observation date, and unconfirmed stock.
- Supplier codes appear separately with a no-warehouse-match label.
- No horizontal overflow or JavaScript page errors at either viewport.
- Conversation identifiers use page memory; refresh creates a fresh session.

Automated DOM checks:

```sh
node tests/automotive_ui_test.cjs
node tests/automotive_chat_ui_test.cjs
node --check automotive/static/app.js
```

The conversation checks cover bilingual message language selection, VIN redaction from the
transcript and URLs, session reuse for follow-ups, safe text rendering, pagination, resetting
pending turns, and deleting a conversation created after a stale request.

Screenshots:

- [Desktop conversation](conversational-parts-desktop.png)
- [Mobile conversation](conversational-parts-mobile.png)
