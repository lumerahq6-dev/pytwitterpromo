# Standalone official X publisher

`official_publisher.py` sends one reviewed text Post or local MP4 through X API v2.
It is separate from the disabled legacy publisher and fleet scheduler. This is a
standalone adapter, tested with fake HTTP responses; no live account or API acceptance
has been verified. Official API use cannot restore an account or guarantee that X
will accept its content or refrain from suspension.

## Required setup

1. Resolve suspension/lock notices with X first. Use the existing, authorized
   account; this adapter does not replace accounts or clear local restrictions.
2. Configure an X developer app with access to the relevant endpoints and any
   required credits/access in the Developer Console. No credits are purchased by
   this adapter. Enable OAuth2 user authorization and register a callback URL.
3. Complete X's Authorization Code flow with PKCE for that account. Request
   `tweet.read tweet.write users.read media.write`; add `offline.access` if you
   want refresh tokens. An app-only bearer token cannot publish. A server app
   needs its client ID/secret for token exchange. This compact adapter accepts an
   existing user access token; OAuth callback hosting and refresh are not included.
4. Supply `X_USER_ACCESS_TOKEN` and `X_EXPECTED_USERNAME` privately in the process
   environment. Use your secret manager or protected shell environment, and keep
   credentials out of chat, command arguments, screenshots, and source control.
   Do not use the browser's `auth_token` cookie. Expired tokens require an authorized
   OAuth refresh/re-authorization before another attempt. Optionally set
   `X_EXPECTED_USER_ID` to pin the account's immutable numeric ID as well as its handle.
5. Install the live-only dependency in the project's Python environment:

   ```bash
   python -m pip install requests
   ```

[OAuth2 setup and scopes](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
and the [official OpenAPI contract](https://docs.x.com/openapi.json) document the
authorization requirements.

## Preview, test, then explicitly publish

Run from this directory. Without `--publish`, all execution is offline: no HTTP,
credential access, or incident writes. The preview prints input sizes rather than
content or secrets.

```bash
python official_publisher.py --text "Your reviewed original caption" --video path/to/clip.mp4
python -m unittest discover -s tests -p test_official_publisher.py -v
```

After the account, app, content rights, links, and labeling have been reviewed,
explicitly send one Post using the privately configured environment:

```bash
python official_publisher.py --publish --text "Your reviewed original caption" --video path/to/clip.mp4
```

Omit `--video` for text only. For genuinely non-sensitive media, add
`--non-sensitive`. Adult-media warning is the default. Regular adult-media
publishers must also configure X's account-level sensitive-media setting and
observe restrictions on profile/header placement. See [X's adult-content
policy](https://help.x.com/en/rules-and-policies/adult-content).

## Behavior and limits

- Before any upload, `GET /2/users/me` must match `X_EXPECTED_USERNAME`. Any matching
  entry in `data/benched.json` blocks publishing, regardless of its reason; malformed
  or unreadable bench data also blocks it. No entry is automatically cleared.
- Video uses the current initialize/append/finalize/status paths, with 4 MiB binary
  chunks, at most 1,000 segments, and a 180-second processing deadline. This adapter
  accepts MP4 only, up to 4,194,304,000 bytes. X's account/content limits still apply.
  [Current media upload sequence](https://docs.x.com/x-api/media/quickstart/media-upload-chunked).
- Adult video requires successful `POST /2/media/metadata` and a response confirming
  `associated_metadata.sensitive_media_warning.adult_content: true` for that media
  ID. An older official example documents this boolean; the current OpenAPI types
  the warning only as an object. This behavior has not been verified live. Missing
  confirmation blocks the Post. Do not bypass that check to publish adult content.
  [Media metadata](https://docs.x.com/x-api/media/create-media-metadata).
- Publishing uses `POST /2/tweets` with `text` and optional `media.media_ids`.
  There is no supported `possibly_sensitive` field in its current request schema.
  [Create Posts](https://docs.x.com/x-api/posts/create-post).
- No automatic retries, redirect following, environment proxies, browser cookies,
  or fallback accounts. A lost create response is `create_outcome_unknown`: inspect
  the account manually before another attempt because the Post may already exist.
- An exclusive `data/official_publisher_holds/<lowercase-username>.json` prevents
  concurrent invocations. An interrupted run, unknown create outcome, or explicit
  restriction code 64/326/226/344 retains this hold. A later invocation refuses
  networking while it exists. After manually reconciling the account, notices, and
  posted content, an operator may archive the hold outside this directory; there is
  no automated retry or command-line bypass. Do not clear it just to retry a rejection.
- Accepted Posts are recorded in `data/publishing_receipts.jsonl` before clearing
  the hold. Receipts contain the account/user ID, timestamp, Post ID, and SHA-256
  hashes of text/video, with no token or content. Receipt-write failures retain the
  hold and report the known accepted Post ID in the redacted incident log.
- Errors append a redacted, bounded stage record to `data/publishing_incidents.jsonl`.
  Code 64 means suspended, 326 locked, 226 an automation/spam rejection, and 344 is
  retained as unknown. A generic HTTP 403 is `forbidden`, not proof of suspension.
  Rejections stop this invocation. No periodic publisher is enabled.
  [Official error guidance](https://docs.x.com/x-api/fundamentals/response-codes-and-errors).

Do not use this adapter for duplicate promotion across accounts. Review the
[automation rules](https://help.x.com/en/rules-and-policies/x-automation) and
[authenticity rules](https://help.x.com/en/rules-and-policies/authenticity).
