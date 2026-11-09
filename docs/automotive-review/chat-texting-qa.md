# Texting-style parts chat: UI verification

Checked in Chrome against the real local app (real catalog and providers, port 8768), with
typed cars only so no VIN appears on screen. Desktop 1440 × 1100 and phone 390 × 844.

## Conversations exercised

English: `hi there` → `I need front brake pads for a Honda Accord` → `2015` →
`is the second one in stock?` → `thanks!`. Replies arrived in 55-811 ms:

- "Hi! What part are you looking for?"
- "Got it, a Honda Accord. What year is it?"
- the lookup reply with the count, where the items are (store or warehouse in 1-2 hours), one
  note and "Need the rear ones too?";
- "That's the Sato one (30002960). It's at our warehouse and can be in the store in 1-2 hours."
- "You're welcome! Anything else for your 2015 Honda Accord?" — no new search.

Georgian (phone): `გამარჯობათ` → `ხუნდები მჭირდება 2015 წლის ჰონდა აკორდისთვის`. The reply
names the car and count and asks "წინა თუ უკანა გჭირდებათ?" with წინა / უკანა chips under it.

## Checks

- Messenger bubbles (customer right, assistant left), no "You"/"Assistant" headings.
- Typing indicator until the first cards or the reply; a friendly progress line instead of
  raw source states.
- One short note per reply at most; card warnings show one line, the rest under *Details*.
- No "Price: unknown" line; match source, matched codes and observation date under *Details*.
- Quick-reply chips sit under the reply text and send the chip text as the next message.
- Enter sends, Shift+Enter adds a line; the reply-language selector now has a valid Georgian
  option (it was missing its opening tag).
- Found during this check and fixed: the HTTP response model dropped the new `note`,
  `quick_replies`, `act` and `language` fields, so the browser fell back to listing every
  warning. A regression test now covers the fields end to end.

Automated DOM checks: `node tests/automotive_chat_ui_test.cjs` and
`node tests/automotive_ui_test.cjs`.

Screenshots:

- [Desktop conversation](chat-texting-desktop.png)
- [Phone conversation (Georgian)](chat-texting-mobile.png)
