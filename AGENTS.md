# Pullarr agent instructions

## Scope and priorities
- Lightweight Python/Docker app: monitor YouTube RSS, download with yt-dlp, manage subscriptions through a web UI, persist state in SQLite.
- Prioritize reliability, data preservation, correct channel identity, low resource usage, maintainability, security, usability, and compatibility.
- Prefer standard-library solutions, focused functions, existing conventions, sequential downloads, and minimal dependencies. Avoid unnecessary rewrites, services, abstractions, duplicate functionality, and dead code. Log contextual errors; never silently hide failures.

## Versions and delivery
- Use `MAJOR.FEATURE.BUGFIX` (for example, `v0.0.0`): first number = major release, second = new feature, third = bugfix. Increment FEATURE for new features and BUGFIX for bug fixes without requiring a separate versioning request. Reset lower numbers when incrementing a higher number. Keep all version references consistent.
- Update README, changelog, and example configuration when behavior/configuration changes; document migrations and non-obvious behavior.
- Never modify the user's active Compose configuration or existing volume mappings. Ship only `docker-compose.example.yml`; exclude `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, and `compose.yaml` from releases.
- Preserve `/config`, `/downloads`, default web port `8080`, and `docker compose up -d --build pullarr`. Prefer an unprivileged container user; document breaking Docker changes.

## Behavior and data
- Manage subscriptions in the UI, not YAML. Accept channel URLs, `@handles`, and `UC...` IDs; validate canonical channel identity before saving and display the actual title. Never trust the first channel ID in HTML; it may belong to a recommendation.
- Prevent duplicate subscriptions. Provide enable/disable and removal controls. Disabled channels must not be polled or downloaded; removing a subscription must preserve downloaded media.
- Use RSS as the primary discovery method and yt-dlp for resolution/downloads. Persist discovered video IDs and download history; prevent repeat downloads and duplicate retry jobs. Retry failures gracefully, including unavailable/deleted videos, network errors, and rate limits.
- Save under the configured download directory (default `/downloads`) using `Channel Title/Season YYYY/Channel Title - SYYYYEMMDD - Episode Title.ext`. Sanitize paths, use deterministic names, preserve existing files, and confirm output exists before marking a download successful.
- Preserve subscriptions, history, and restart recovery. Use backward-compatible SQLite migrations tested against older databases; never recreate/delete the database on upgrade or unnecessarily alter history.

## UI and security
- Follow the saved palette and layout preferences in `UI_DESIGN.md` when changing the UI.
- Keep the UI lightweight, responsive, and visually consistent on mobile/desktop; use compact controls and fixed-size toggles. Show useful errors, avoid unnecessary page reloads, and confirm destructive actions such as removal.
- Validate URLs/IDs, prevent path traversal and shell injection, use network timeouts, safely render untrusted text, and keep secrets/sensitive files out of logs, API responses, and HTTP serving.

## Validation
- After every code modification, run `PYTHONPATH=. python -m unittest discover -s tests -v`, available linters, and syntax checks. Check unused/undefined symbols, types where supported, deprecated APIs, and security issues.
- Run Fallow after every modification, covering changed code and the wider project. Consult `fallow --help`; never invent flags. Fix actionable duplication, dead code, complexity, obsolete code, and maintainability issues. Explain false positives or limitations; do not suppress warnings just to pass.
- Add regression tests for bug fixes. Cover URL/handle/ID resolution, RSS validation/polling, toggles/removal, duplicate prevention, downloads/naming, persistence, and restart recovery as relevant.
- When Docker is available, build the image, verify startup/logs and persistent volumes. Otherwise report Docker verification as unavailable.
- Rerun affected checks after fixes until introduced actionable issues are resolved. Never claim checks passed unless executed.

## Releases and reporting
- Before releases: complete tests, static analysis, Fallow, database compatibility and available Docker checks; fix actionable issues, update changelog, verify versions, exclude active Compose files, and package as `pullarr`.
- Final response: briefly state changes, affected files/components, executed checks, Fallow results, unresolved limitations, and version if changed.
- After modifications, always include an explicit list of files and folders that must be uploaded to the server, using paths relative to the project root. Include required rebuild/restart commands, and distinguish deployment files from optional documentation or tests. Remind the user to preserve their active Compose configuration, persistent database, and downloaded media when relevant.
