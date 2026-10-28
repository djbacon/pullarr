# Changelog

## v0.16.1

- Keep section URLs clean during automatic version refreshes; remove legacy `v` and `_refresh` parameters without reloading, preserving the section and unrelated query parameters.
- Retain no-store HTTP responses, version polling, and bounded cache cleanup. No configuration or database migration is required.

## v0.16.0

- Add live count badges to the Channels and Downloads navigation items, including mobile navigation. Channels counts all subscriptions; Downloads counts queued and active jobs, including paused channels and retries, independently of the completed-history toggle.
- Reuse the overview refresh every five seconds while the page is visible to keep counts updated across all sections. No configuration or database migration is required.

## v0.15.5
- Make Downloads progress bars 28px high and center the download/conversion status and percentage inside. Use blue fill and white text for readable contrast.
- Retain accessible native progress values and indeterminate progress when the percentage is unavailable. No database migration or configuration change is required.

## v0.15.4
- Match Queue & downloads to Home with separate Video, Channel, and Status columns. Keep live progress, retry/disabled notes, history filtering, and confirmed removal in the Status column.
- No database migration or configuration change is required.

## v0.15.3
- Persist downloaded source paths, original directories, and encoding choices before starting conversion; resume them locally after rebuild/restart instead of restarting YouTube lookups at 0%.
- Recover older published conversions using retained staging ready hard links; preserve existing media and refuse ambiguous recovery.
- Skip already completed IDs before starting a download. Add the backward-compatible download_jobs table without resetting subscriptions/history.

## v0.15.2
- Remove the sidebar downloader/version footer and repeated top-bar branding. Show one small version number beside the Pullarr logo text.
- No database migration or configuration change is required.

## v0.15.1
- Ensure automatic version refresh proceeds even when browser cache cleanup stalls; limit cleanup to three seconds and attempt all cache deletions independently.
- Avoid duplicate refreshes while navigation is pending. Retain HTTP cache purge requests, uncached version checks, cache-busting URLs, and the selected section.
- No database migration or new configuration is required. Browser HTTP cache purging still depends on HTTPS and browser support.

## v0.15.0
- Redesign the dark UI using the saved five-color palette, left navigation, compact channel/download tables, and aligned settings rows.
- Add a live Home overview with channel, queue, and active-job counts and queue activity. Support section links and browser history; adapt navigation and tables for mobile.
- Preserve channel profiles, controls, download progress, settings, and all persistent state. No database migration or configuration change is required.

## v0.14.0
- Show live conversion percentage and a progress bar in the download queue while the next video downloads.
- Read ffmpeg progress against probed source duration, reserve 100% for validated published output, and show indeterminate progress when duration is unavailable. Preserve bounded diagnostics, conversion priority, retries, and restart recovery.
- No database migration or configuration change is required.

## v0.13.0
- Download the next video while the current video converts, with one downloader, one encoder, and at most one next source waiting for the encoder.
- Show simultaneous downloading and conversion in the queue and channel profiles. Keep SQLite writes on the main worker thread and preserve per-job settings, retries, restart recovery, and sequential conversion.
- Wait for final conversion, output confirmation, and history commit before empty-queue staging cleanup. No database migration or new configuration is required.

## v0.12.0
- Automatically reclaim leftover download and conversion staging files when the queue is empty after a polling cycle, including after restart.
- Preserve temporary inputs for failed, pending, and disabled-channel queued jobs; skip cleanup during active work or shutdown. Only the current download root’s `.pullarr-partial` directory is cleaned, without following staging symlinks or touching completed media.
- Log cleanup errors and retry next poll. No database migration or configuration change is required.

## v0.11.1
- Run video conversion at the lowest CPU scheduling priority (nice 19) and idle disk I/O priority, preserving the app's normal priority and existing encoder limits.
- Include priority utilities in Docker; warn when native installations lack them. No database migration is required.

## v0.11.0
- Add channel profiles with paginated upload tables, YouTube links, lengths, and live download statuses.
- Browse a persistent public upload catalog and queue selected older videos without downloading the entire channel.
- Preserve existing history and worker policies; add an independent SQLite catalog table.

## v0.10.0
- Add a persistent Skip Shorts toggle in Settings, enabled by default for new and existing installations.
- Check Shorts before queued downloads; remember skipped IDs across restarts and retry lookup failures.
- Disabling the filter requeues skipped Shorts within the current selection; preserve completed media and history.

## v0.9.0
- Detect server version changes in open pages and reload automatically with a cache-busting URL.
- Clear browser Cache Storage and request an HTTP cache purge on upgrades; keep all API requests uncached.
- Retry version checks after temporary outages and check again when a tab becomes visible.

## v0.8.0
- Add persistent Original, x264 (SDR), and x265 (10-bit HDR10/HLG) encoding settings for new downloads.
- Convert sequentially with ffmpeg, preserving resolution, color tags and available static HDR metadata; use AAC audio in MKV.
- Retain failed conversion sources for retry, validate output before completion, and never overwrite existing media.
- Show conversion status in the download queue; preserve existing files, settings, and history.

## v0.7.0
- Add verified channel metadata (`tvshow.nfo`), avatar posters, and channel backgrounds without replacing existing sidecars.
- Generate episode NFOs for Plex NFO Series and backfill enabled subscriptions under the current download root without re-downloading media.
- Persist daily metadata checks and hourly failure retries in an additive SQLite table; preserve subscriptions, history, media, and user artwork.
- Document Plex NFO Series setup to avoid incorrect online TV-show matches.

## v0.6.0
- Save each new episode’s YouTube thumbnail as a matching JPEG sidecar for Plex local artwork, using the included ffmpeg for conversion.
- Complete thumbnail processing before Plex scan notifications; log missing artwork without discarding a successful video download.
- Document Plex local-assets settings and the new-download-only scope.

## v0.5.0
- Automatically update yt-dlp and its default dependencies at startup and every 24 hours, retrying failures hourly.
- Stage and validate updates separately; preserve the working version and active operations on failure, and use fresh subprocesses for updated metadata lookups and downloads.
- Keep the unprivileged Docker user and existing volumes/database unchanged.

## v0.4.0
- Add Plex notification settings with server/token/library configuration, container path mapping, and a saved-settings connection test.
- Request a targeted folder scan after successful downloads; persist and retry failed notifications independently of media downloads.
- Keep Plex tokens out of URLs, API responses, and logs; block credential forwarding through redirects.

## v0.3.0
- Organize new downloads in Season YYYY folders and use SYYYYEMMDD episode names, including dates resolved by yt-dlp for archive downloads.
- Add a Show downloaded videos toggle and confirmed removal of completed video files, preserving IDs to prevent automatic downloads of removed videos.
- Add a Settings tab for a persistent default download directory shared by all channels, with writable-path validation and immediate use by the next download.
- Fall back from failed RSS requests to recent upload discovery for all download selections, with verified publication times and cached dates.
- Remove the alternate RSS URL that returned feeds without channel identity.
- Add Channels and Downloads tabs with a live queue, downloading percentage, progress bars, and automatic refresh.
- Stream yt-dlp progress while preserving persistent queue/retry state; show disabled channels and retries in the queue.
- Add per-channel download selection: All, Latest and future (default), or Only future videos.
- Discover full upload history with yt-dlp for All, beyond the RSS window.
- Persist selections with a backward-compatible SQLite migration; replace the global first-run environment setting.
- Apply selection changes to queued work while preserving completed downloads; retry pending downloads during feed outages.
- Verify output files before marking downloads complete, and resolve archive upload dates during download.

## v0.2.3
- Preserve channel titles from yt-dlp or channel page metadata so RSS failures do not prevent adding verified channels.
- Fall back to channel metadata for direct channel IDs and URLs when RSS is unavailable or has no title.
- Reject non-Atom and mismatched feed responses instead of silently treating them as empty feeds.
- Read feed-level titles for channels with no recent entries.

## v0.2.2
- Fix falsely resolved YouTube handles caused by unrelated channel IDs on YouTube pages.
- Confirm the RSS feed works and supplies a channel title before adding a subscription; surface failures in the UI.
- Show the channel title from RSS rather than only the channel ID.
- Fix stretched channel toggle by removing the generic input flex style from switches.
- Add Remove action for clearing incorrectly added channels without deleting downloaded files.
- Refresh legacy channel titles from a valid RSS response.

## v0.2.1
- Improved YouTube @handle resolution with additional page metadata patterns and yt-dlp fallback.
- Improved error message for blocked handle lookups.

## v0.2.0
- Replaced YAML channel configuration with an embedded web UI.
- Add channels by ID, @handle, or channel URL.
- Persist subscriptions in SQLite and toggle them without restart.
- Added HTTP port to example Compose configuration. Removed config example.

## v0.1.0
- Initial feed polling, video downloading, persistent history and per-channel file naming.
