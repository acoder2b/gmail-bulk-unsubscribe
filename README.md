# Gmail Bulk Unsubscribe

A command-line tool for cleaning up a Gmail inbox from the command line — organizing, trashing, and now **bulk-unsubscribing** from mailing lists without a browser.

This is a fork of [mxngls/Gmail-Cleaner](https://github.com/mxngls/Gmail-Cleaner) (MIT licensed). The original project handles listing top senders, trashing/spam-flagging by sender or label, and batch processing with rate-limit backoff. This fork keeps all of that and adds the one thing that was missing: **actually unsubscribing**, not just deleting.

## What's new in this fork

Gmail's own "Unsubscribe" button isn't a Gmail feature — it reads the `List-Unsubscribe` / `List-Unsubscribe-Post` headers a compliant sender includes on bulk mail (RFC 2369 / RFC 8058) and fires a single background HTTP POST. As of 2024, Google *requires* high-volume senders to support this one-click method or risk being flagged as spam — which means a large share of newsletter and marketing mail can be unsubscribed from programmatically, with no browser and no clicking through a mailing-list host's own web page.

New menu option: **"Bulk-unsubscribe from one or more senders."** Give it one sender or a comma-separated list, and for each one it will:
- Pull the `List-Unsubscribe` headers from one representative message (cheap metadata-only fetch, not a full download)
- If one-click (RFC 8058) is supported → fire the POST directly and confirm
- If only a plain web link is available → report the link back to you rather than guess at loading an unknown page automatically
- If only a `mailto:` address is available → report it rather than send an email on your behalf automatically (sending mail is a meaningfully different, riskier action than reading your own inbox, so this tool stops short of doing it without you)
- If the sender doesn't support `List-Unsubscribe` at all → tell you plainly instead of silently doing nothing

Everything is reported back in a clear summary — unsubscribed, needs a manual link, needs an email, not supported — so you know exactly what happened to each sender, not just a generic "done."

## Prerequisites
1. Python 3.13+ (managed automatically if you use `uv`)
2. A Google account with Gmail enabled
3. [uv](https://github.com/astral-sh/uv) for package management (recommended) or pip

### Enabling the Gmail API
- Create a [Google Cloud Platform project with the Gmail API enabled](https://developers.google.com/workspace/guides/create-project)
- Create an [OAuth client ID (Desktop app type)](https://developers.google.com/workspace/guides/create-credentials) and download the credentials JSON

### Installing dependencies
With uv (recommended):
```bash
uv sync
```

With pip:
```bash
pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib requests
```

## Installation
```bash
git clone https://github.com/acoder2b/gmail-bulk-unsubscribe.git
cd gmail-bulk-unsubscribe
```

## Usage
1. Rename the OAuth client JSON you downloaded to `credentials.json` and place it in the `src/` folder
2. Run the script from `src/`:

```bash
cd src
uv run --project .. python main.py
```

(First run opens a browser for you to approve access — this creates a local `token.json` so you won't need to re-authenticate on later runs. Since the app is unverified by Google, you'll see an "unsafe" warning the first time; that's expected for a personal script like this one — click through it.)

The script offers:
1. Show the most common senders
2. Move messages from a specific sender to trash (batch processing)
3. Move messages from a specific sender to spam
4. Move all spam to trash
5. Move messages matching a label to trash
6. Add a label to emails from a specific sender
7. **Bulk-unsubscribe from one or more senders** (new)
8. Exit

### Bulk queries in a single run
Every "sender" prompt in this tool is just concatenated onto `from:`, so you can pass full Gmail search syntax, not just a plain address — e.g. `sender.com after:2023/01/01`, or chain multiple senders with `OR from:` to process them in one run: `a.com OR from:b.com OR from:c.com`.

## Key Features
- **Bulk unsubscribe** via RFC 8058 one-click `List-Unsubscribe-Post`, with an honest fallback report for senders that don't support it (new in this fork)
- **Batch processing**: bulk operations use Gmail's batch API to optimize performance and stay within API rate limits
- **Retry with backoff**: exponential backoff with jitter on rate-limit (429/403) and server (5xx) errors, including per-item retry for the sender-scanning operation, not just whole-batch retry
- **Real-time progress**: progress bars for all long-running operations
- **Modern dependency management**: uv with a locked `pyproject.toml`/`uv.lock`

## Known Limitations
- Very large mailboxes take real time to scan — Gmail's per-user API rate limit (250 quota units/second) is a hard ceiling, not something this tool can bypass, only cooperate with via backoff
- Bulk-unsubscribe can only automate what the sender supports — a plain web-form unsubscribe link still needs a browser; this tool tells you which senders fall into that bucket instead of guessing
- Credentials (`credentials.json`, `token.json`) are gitignored and stay local — never commit them

## Credits
Built on top of [mxngls/Gmail-Cleaner](https://github.com/mxngls/Gmail-Cleaner) — batch processing, rate-limit handling, and the core sender/label/trash operations are from the original project. This fork adds the bulk-unsubscribe feature and its accompanying header-parsing/one-click-POST logic.

## License
MIT — see [LICENSE](LICENSE). Original copyright retained per the license terms; this fork's additions are made available under the same license.
