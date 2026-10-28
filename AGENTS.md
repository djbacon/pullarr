# Pullarr agent instructions

- Lightweight Python/Docker app: monitor YouTube RSS, download with yt-dlp, manage subscriptions through a web UI, persist state in SQLite.

## Always apply
- Prioritize reliability, data preservation, correct channel identity, low resource usage, maintainability, security, usability, and compatibility.
- Prefer standard-library solutions, focused functions, existing conventions, sequential downloads, and minimal dependencies. Avoid unnecessary rewrites, services, abstractions, duplicate functionality, and dead code. Log contextual errors; never silently hide failures.
- Preserve subscriptions, history, downloaded media, and restart recovery. Never modify the user's active Compose configuration or existing volume mappings.
- Validate URLs/IDs, prevent path traversal and shell injection, use network timeouts, safely render untrusted text, and keep secrets/sensitive files out of logs, API responses, and HTTP serving.
- After completing each set of modifications and required validation, automatically create a Git commit without asking for confirmation. Include only changes made for the current task, use a clear message describing the result, and report the commit hash. Do not create empty commits or include unrelated user changes. Commit locally; push only when explicitly requested.

## Read detailed rules only when relevant
These files contain mandatory instructions. Read the applicable files before working; do not load the entire directory by default. Paths are relative to this file.
- App behavior, channel identity, RSS, downloads, paths, or SQLite: [data rules](docs/agent-rules/data.md).
- UI changes: [UI rules](docs/agent-rules/ui.md) and [design preferences](UI_DESIGN.md).
- Any modification: [validation rules](docs/agent-rules/validation.md) and [delivery/reporting rules](docs/agent-rules/delivery.md). Apply their checks according to the kind of change.
