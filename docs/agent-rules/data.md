# Behavior and data

- Manage subscriptions in the UI, not YAML. Accept channel URLs, `@handles`, and `UC...` IDs; validate canonical channel identity before saving and display the actual title. Never trust the first channel ID in HTML; it may belong to a recommendation.
- Prevent duplicate subscriptions. Provide enable/disable and removal controls. Disabled channels must not be polled or downloaded; removing a subscription must preserve downloaded media.
- Use RSS as the primary discovery method and yt-dlp for resolution/downloads. Persist discovered video IDs and download history; prevent repeat downloads and duplicate retry jobs. Retry failures gracefully, including unavailable/deleted videos, network errors, and rate limits.
- Save under the configured download directory (default `/downloads`) using `Channel Title/Season YYYY/Channel Title - SYYYYEMMDD - Episode Title.ext`. Sanitize paths, use deterministic names, preserve existing files, and confirm output exists before marking a download successful.
- Preserve subscriptions, history, and restart recovery. Use backward-compatible SQLite migrations tested against older databases; never recreate/delete the database on upgrade or unnecessarily alter history.
