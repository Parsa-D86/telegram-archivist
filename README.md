# Telegram Archivist

Telegram Archivist is an interactive terminal application for archiving media from Telegram chats that your account can access. It uses your Telegram user account through Telethon's MTProto API; it is not a Telegram bot.

## Features

- Archive music, videos, video messages, GIFs, voice messages, images, files, and stickers.
- Scan selected chat history using Telegram search filters and keep an incremental SQLite index.
- Resume a run by skipping completed downloads that are still present on disk; failed items are tried again on a later run.
- Use SOCKS5, SOCKS4, or HTTP proxies.
- Save account and proxy profiles for later interactive selection.
- Browse prompts, scan progress, download status, and summaries in a Rich terminal interface.

Images include Telegram photos and image files. Files are organized into subfolders such as video files, documents, archives, audio files, apps, and other files.

## Requirements

- Python 3.10 or newer.
- Linux, Windows, and macOS are intended targets. The maintainer has used the application live on Linux; Windows and macOS have not been verified. The code uses cross-platform path handling and sanitizes filenames for common filesystem restrictions, but filesystem behavior can still vary by operating system.
- Dependencies are pinned in `requirements.txt`: Telethon, Rich, Mutagen, and `python-socks[asyncio]`.

## Installation

Run these commands from the project directory.

### Linux and macOS

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

If PowerShell does not allow activation scripts, use Command Prompt instead:

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

## First run

1. Enter your Telegram API ID and API hash. Obtain your own credentials at [my.telegram.org](https://my.telegram.org) under **API development tools**.
2. Choose whether to use a proxy. Supported types are SOCKS5, SOCKS4, and HTTP; a proxy username and password are optional.
3. Sign in to your Telegram account. On the first login, Telegram asks for your phone number and the login code it sends, plus your two-step verification password if enabled. Later runs use the local session.
4. Choose a chat using its numeric ID, `@username`, a Telegram link, or `1` for Saved Messages. Your account must already be able to access the chat.
5. Select one or more media categories.
6. The application scans the selected history and shows a summary.
7. Confirm with `y` to download, `n` to skip, or `l` to list the queued items before deciding.
8. The application downloads the selected items and reports successes and failures.

## Download locations and local data

Files are placed under the project directory:

```text
Downloads/<chat title> [<chat id>]/
├── Music/
├── Videos/
├── Video messages/
├── GIFs/
├── Voice messages/
├── Images/
├── Stickers/
└── Files/
    ├── Video files/
    ├── Documents/
    ├── Archives/
    ├── Audio files/
    ├── Apps/
    └── Other/
```

Runtime data is kept in `Data/`: `archive.db` is the SQLite index and download history, including chat identifiers and media metadata; `profiles.json` stores saved profiles (including the account display name and proxy host/username); and Telethon creates session files such as `session_<api_id>.session`. Logs are written to `Logs/archivist.log` with rotated backups and may include chat identifiers or error details.

**Never commit or upload `Data/`, `Downloads/`, `Logs/`, or any Telegram session file.** The session file grants access to a logged-in Telegram session. `Data/profiles.json` stores the Telegram API hash and any proxy password in plain text, along with profile labels and proxy connection details. The index and logs can contain identifying information about chats or media. The application requests owner-only permissions for the data directory, session file, and profile file where supported; this is best effort and does not guarantee protection on every operating system or filesystem. Protect these files as account credentials and personal data.

## Re-runs, duplicates, and failures

- The SQLite index tracks the newest message scanned for each Telegram search filter. Later scans focus on newer messages. Choosing a full rescan clears that chat's media index and scan checkpoints.
- Completed downloads are skipped when the indexed output path still exists. If you move or delete a downloaded file, it may be queued again.
- Duplicate detection compares the media kind, file size, duration, dimensions, and original filename reported by Telegram. It keeps one copy in the download queue. Files classified as Files are never deduplicated.
- Downloads are written to a temporary `.part` file and renamed after completion. An interrupted or failed partial file is removed; the application does not continue from that partial file. Completed files remain and are skipped on a later run. Failed items are retried during the run and can be tried again on a later run.
- Existing music tags are preserved. Telegram title and artist metadata are added only to empty fields when Mutagen can write them.

## Troubleshooting

- **Login fails:** confirm the API ID and hash, check the phone number and login code, and enter your two-step verification password if requested. If using a proxy, verify its address, port, credentials, and availability.
- **Proxy connection problems:** retry without the proxy or verify that the proxy supports the selected protocol and can reach Telegram.
- **FloodWait messages:** Telegram is limiting requests. The client automatically waits for shorter FloodWait periods; longer waits may be shown to you. Wait before retrying and avoid repeatedly restarting scans.
- **Insufficient disk space:** the application stops when it detects a full disk. Finished files are retained; make space and run it again.
- **Connection drops / update-loop workaround:** after connecting, the application cancels Telethon's private `_updates_handle` because the background update loop caused a connection crash with Telethon 1.45.0 in the maintainer's experience. The application does not use live updates. This depends on an internal Telethon attribute that may change or disappear in a future version; such a change could break the workaround.

## Testing status

The maintainer reports offline checks using stand-ins for Telethon and Rich, and live use on Linux. This project does not claim comprehensive automated tests, CI coverage, or verified Windows/macOS support.

## Limitations and responsible use

- Telegram only; this is not affiliated with Telegram.
- Selecting stickers scans the full chat history because this implementation has no Telegram search filter for stickers. This can take a long time.
- Documents classified as Files are not deduplicated.
- A few private Telethon attributes are used, including `_updates_handle`, which carries compatibility risk.
- Supply your own Telegram API credentials and archive only chats your account is authorized to access. Follow Telegram's Terms of Service and applicable laws. This application does not grant access to chats or bypass platform restrictions.

## License and disclaimer

This project is licensed under the MIT License. See `LICENSE` for the full text.

Use the application at your own risk. Telegram and Telethon are third-party projects; Telegram Archivist is not endorsed by or affiliated with Telegram.
