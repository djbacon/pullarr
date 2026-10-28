# Versions and delivery

- Use `MAJOR.FEATURE.BUGFIX` (for example, `v0.0.0`): first number = major release, second = new feature, third = bugfix. Increment FEATURE for new features and BUGFIX for bug fixes without requiring a separate versioning request. Reset lower numbers when incrementing a higher number. Keep all version references consistent.
- Update README, changelog, and example configuration when behavior/configuration changes; document migrations and non-obvious behavior.
- Never modify the user's active Compose configuration or existing volume mappings. Ship only `docker-compose.example.yml`; exclude `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, and `compose.yaml` from releases.
- Preserve `/config`, `/downloads`, default web port `8080`, and `docker compose up -d --build pullarr`. Prefer an unprivileged container user; document breaking Docker changes.

## Releases and reporting

- Before releases: complete tests, static analysis, Fallow, database compatibility and available Docker checks; fix actionable issues, update changelog, verify versions, exclude active Compose files, and package as `pullarr`.
- Final response: briefly state changes, affected files/components, executed checks, Fallow results, unresolved limitations, and version if changed.
- After modifications, always include an explicit list of files and folders that must be uploaded to the server, using paths relative to the project root. Include required rebuild/restart commands, and distinguish deployment files from optional documentation or tests. Remind the user to preserve their active Compose configuration, persistent database, and downloaded media when relevant.
