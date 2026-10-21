# Changelog

## 0.2.0

- New operation **Inbox → Send Test Code** (`POST /inboxes/{id}/test-code`): puts one sample OTP email into the inbox without going through the mail server, so you can try a Wait for OTP without real email. Returns `message_id` and `sent_at`. Limited to 5 per inbox and 20 per account per hour.
- Wait for OTP / Wait for Link: an empty **Since** field now means `"0"` (the default) instead of sending an empty `since=`, which the API read as "request start time" and could miss a code that arrived before the wait began.

## 0.1.0

- Initial release.
- `mailsocket` action node: Inbox (Create / Delete / List), Message (Wait for OTP / Wait for Link / Get Latest / List).
- `mailsocket API` credential with a live credential test.
- The `mailsocket Trigger` (polling) node is planned for v0.2.0 and is not part of this release.
