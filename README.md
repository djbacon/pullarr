# Pullarr v0.16.1

Lightweight YouTube RSS auto-downloader with a built-in web interface. No YAML configuration file is needed.

## Interface

Use the left navigation for **Home**, **Channels**, **Downloads**, and **Settings**. Menu badges show all subscriptions for Channels and queued/active jobs for Downloads (including paused channels and retries, excluding completed history). Counts refresh every five seconds while the page is visible and when their lists refresh. A small version number appears beside the Pullarr logo text in the sidebar. Home shows live subscription counts, enabled channels, queued videos (including paused channels/retries), active downloads/conversions, and the first eight queue entries. It refreshes every five seconds while visible. Channels and Downloads use compact tables. Home activity and Queue & downloads both use Video, Channel, and Status columns. Downloads shows thick progress bars with the status and percentage centered inside, such as “Downloading 30%” or “Converting 42.5%”. Settings uses aligned compact rows. Navigation supports direct `#home`, `#channels`, `#downloads`, and `#settings` links and browser back/forward. On small screens navigation moves to the top and tables scroll horizontally.

The saved design preferences and exact dark-theme palette are in `UI_DESIGN.md`. This update needs no database migration or configuration changes.

## Docker setup

Use `docker-compose.example.yml` as a **reference** when adding the service to your own Compose setup. No active `compose.yaml` or `docker-compose.yml` is included.

Example service:

```yaml
services:
  pullarr:
    build:
      context: ./pullarr
    container_name: pullarr
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Riga
      POLL_INTERVAL_SECONDS: 900
    volumes:
      - ./pullarr-config:/config
      - /mnt/data/youtube:/downloads
```

Place the application files in `./pullarr` next to your own Compose file, then run `docker compose up -d --build pullarr`.

Open `http://YOUR_SERVER_IP:8080`. Add a channel by `UC...` ID, `@handle`, or `https://www.youtube.com/@handle` / `https://www.youtube.com/channel/UC...`. Toggle channels on or off using the switch. Changes persist in `/config/state.db` and are applied without restarting the container.

Open pages check the server version every 30 seconds while visible, and when returning to the page. After a version change, Pullarr clears browser Cache Storage and requests an HTTP cache purge, then automatically reloads the page, keeping clean section links such as `/#downloads`. Legacy `v` and `_refresh` URL parameters are removed without reloading or changing the selected section; unrelated query parameters are preserved. Cache Storage cleanup is limited to three seconds so blocked or stalled browser APIs cannot prevent the refresh; every cache deletion is attempted even if another fails. HTTP cache purging depends on browser support and HTTPS; no-store responses and version requests also work over plain HTTP. Unsaved form edits are lost on refresh. Saved settings, subscriptions, and downloads are preserved. Pages opened before v0.9.0 need one manual refresh to enable detection.

Completed video IDs, file paths, and statuses are saved in `/config/state.db`. Keep the same persistent `/config` and `/downloads` mounts when rebuilding with `docker compose up -d --build pullarr`; a custom `DATABASE_PATH` must also be on persistent storage. Keep the actual host folders and database, including any SQLite WAL files; use a stopped service or a SQLite backup when copying the database. Downloaded history prevents rediscovery from queuing completed videos again.

Downloaded sources awaiting conversion/completion are recorded before encoding starts. After restart they resume locally with their original directory and encoding setting, without another Shorts lookup or yt-dlp download. Published conversions left by older releases are recovered only when the retained `.ready.mkv` staging hard link proves that it is the same final output. This avoids trusting a matching filename. The additive `download_jobs` table preserves subscriptions and history. Interrupted encodes without a validated ready file restart encoding from the saved source. If both source and ready output are missing, Pullarr logs the loss and retries downloading; keep the `.pullarr-partial` directory while work is pending.

Click a channel title to open its **profile**. The table shows checkboxes, video IDs, titles linking to YouTube, lengths, and **downloaded / queued / downloading / not downloaded** status. Click **Refresh video list** to fetch the channel’s full public upload history with yt-dlp; this may take several minutes. A channel without a saved upload catalog loads its history on first opening. The saved catalog survives restarts; browsing never queues videos. Select up to 100 available videos on the current page and click **Queue selected** to download older uploads without choosing **All**. Pages contain 100 videos; selections reset on page changes. Status refreshes every five seconds while the profile is visible. Unknown lengths display **Unknown** without fetching each video's details.

Disabled channels show saved results but cannot refresh or queue videos. Private/deleted/restricted uploads may be absent from YouTube's listing; previously known videos remain visible. Manual selections use the existing sequential worker, Shorts filter, encoding, filenames, retry handling, and download history. Existing queued/downloaded videos cannot be selected again. Manually selecting a removed video explicitly allows it to download again. Changing the channel's download mode still cancels pending work as before. Upgrades add only a `channel_videos` catalog table; existing subscriptions and download history are preserved. No new configuration is required.

The **Downloads** section lists queued videos from every subscribed channel, with the active download first. It refreshes every two seconds while visible and shows **Downloading** with a percentage/progress bar, or **Queued**. Disabled channels and pending retries are labelled. Completed videos are hidden by default; enable **Show downloaded videos** to include them, even after their channel is removed. **Remove** on a completed video asks for confirmation, deletes its recorded file, and removes it from the list. Its ID remains in history so future scans do not download it again. Active and queued videos cannot be deleted with this action. Queue state survives restarts. Progress follows yt-dlp's current media stream, so it can restart when switching from video to audio and remain at 100% while merging.

Use **Settings → Default download directory** to choose the shared destination for all channels. The initial default is `DOWNLOAD_ROOT`, or `/downloads` if unset. A saved setting takes precedence over the environment variable and persists in SQLite. Pullarr creates the directory if needed and checks write access before saving. The next download, including an already queued video, uses the new directory; active downloads finish in their original directory and existing files are not moved.

**Settings → Skip Shorts** is enabled by default, including on upgrade. Click **Save** after changing it. Before each queued download, Pullarr checks YouTube’s Shorts URL with a timed HEAD request; ordinary watch-page redirects are downloaded. Failed or unexpected responses retry normally rather than silently skipping a video. Skipped IDs persist across restarts and are hidden from the download queue. Turning the setting off queues previously skipped Shorts again within the current channel selection; disabled channels still wait. Changing a channel’s download selection resets skipped work to that new scope. Existing files and download history are preserved. The setting uses the existing SQLite settings table; no schema migration is required. If the newest upload is a Short, **Latest and future** skips it without searching older uploads for a replacement. Detection depends on YouTube’s redirect behavior.

Use **Settings → Video encoding** to select **Original** (default, unchanged YouTube codec), **x264 / H.264** (8-bit SDR), or **x265 / HEVC** (10-bit, preserves HDR10/HLG and available HDR10 mastering/content-light metadata). x264 deliberately selects YouTube’s SDR rendition; if unavailable, the download fails for retry instead of mislabelling HDR as SDR. Choose x265 for 4K HDR playback. Dynamic HDR10+/Dolby Vision metadata is not preserved by conversion; use Original if needed. Resolution and frame rate are retained. Converted videos use MKV with AAC audio; Original retains source audio. Playback still depends on the client’s codec/container support.

Downloads and conversions use a bounded pipeline: one video converts while the next downloads. Only one download and one conversion run at a time; when the next source finishes downloading, further downloads wait until the encoder is available. Original mode continues downloading sequentially without an encoder. The Downloads section and channel profiles show both active videos. Active work finishes when a subscription is disabled/removed; the worker rechecks subscription controls before starting another download. Each job retains the download directory and encoding setting selected when it started. SQLite updates stay on the main worker thread, and queue-empty maintenance waits for conversion and confirmed publication/commit before cleaning staging. Interrupted staged jobs remain queued for retry after restart. No migration or new configuration is needed.

Encoding runs sequentially on CPU (two encoder threads, medium preset, CRF 20 for x264 / 22 for x265). Conversion runs at the lowest CPU scheduling priority (`nice -n 19`) and idle disk I/O priority (`ionice -c 3`), yielding to other workloads under contention; this is not a CPU usage cap. I/O priority depends on the host's disk scheduler. The Docker image includes both utilities; native installations should install them too (missing utilities produce a warning and the remaining priority settings still apply). Rebuild and restart to apply this to new conversions; no database migration or setting is required. 4K conversion can be slow and needs space for both source and output. The Downloads section shows **Converting · 42.5%** and a progress bar alongside the next video’s download progress. Conversion percentage estimates encoded playback time against the source duration and stays below 100% until output validation and publication finish. If duration is unavailable, the bar is indeterminate; conversion continues normally. Progress is transient and restarts when an interrupted conversion retries; reused completed staging output reports 100%. Inputs stay under `.pullarr-partial` until conversion succeeds; failures retry without marking the video complete. Output is validated and published without overwriting existing media. A conflicting existing filename requires manual resolution. Settings persist in the existing SQLite settings table, defaulting older databases to Original without modifying history. Changes apply to downloads that start after saving; existing library files are not converted.

Enter an absolute **container path**, such as `/downloads/YouTube`. With the example volume mapping, that corresponds to `/mnt/data/youtube/YouTube` on the host. Other locations must be writable by the container user and mounted through your own Compose configuration for persistence. Changing this setting does not change Docker mounts.

Each file is stored under `Default Download Directory/Channel Title/Season YYYY/Channel Title - SYYYYEMMDD - Episode Title.ext` (default format MKV when streams require merging). For example: `Daily Dose Of Internet/Season 2026/Daily Dose Of Internet - S2026E1008 - His Pants Had Other Plans.mkv`. The episode number is the zero-padded month and day. This applies to new downloads; existing files and their recorded paths are preserved. Downloads run sequentially in the background, overlapping with a single conversion when encoding is enabled. Each channel has a **Download videos** selector:

- **All:** queue the full available upload history using yt-dlp, then monitor for future uploads using RSS with an upload-playlist fallback. This is not limited to the recent RSS entries. Private/deleted videos are skipped.
- **Latest and future** (default): download the newest video available at the first successful poll, plus videos published after the selection was made.
- **Only future videos:** download only videos published after the selection was made.

You can also choose the selection in the add-channel form before any download starts. Selections persist across restarts. Changing a selection replaces pending work and retries according to the new scope; downloaded files and history are preserved. A download already running may finish. Disabled channels wait until enabled. Failed downloads retry on later checks, even if RSS is temporarily unavailable. A failed history scan is retried.

Existing databases migrate automatically and existing subscriptions receive the **Latest and future** default. This can queue the latest video if it was previously skipped. The old `DOWNLOAD_EXISTING_ON_FIRST_RUN` environment setting is superseded by the per-channel selector and is no longer used.

Optional environment settings: `POLL_INTERVAL_SECONDS` (minimum 60), `WEB_PORT` (default 8080), `WEB_HOST` (default 0.0.0.0), `DOWNLOAD_ROOT`, `DATABASE_PATH`, and `LOG_LEVEL`.

**Security:** The UI has no login. Expose it only on a trusted LAN or behind authenticated access; do not publish it directly to the internet. Channel resolution requires access to YouTube and may fail if YouTube blocks requests. Download support depends on YouTube and yt-dlp behavior.

## Plex notifications

In **Settings → Plex notifications**, enter:

- **Plex server URL:** reachable from Pullarr, for example `http://plex:32400` on a shared Docker network or `http://192.168.1.10:32400`. `localhost` refers to the Pullarr container.
- **Plex token:** follow [Plex's token instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/). It is stored in `/config/state.db`, sent in a request header, and never returned to the browser. Leave the field blank on later saves to keep it.
- **Library ID:** the numeric section key for your YouTube library. Plex's [`/library/sections` command](https://support.plex.tv/articles/201638786-plex-media-server-url-commands/) lists these keys. The folder must already belong to this library.
- **Download directory as Plex sees it:** map Pullarr's default directory to the equivalent Plex container directory, or leave blank for identical paths. For example, Pullarr `/downloads` → Plex `/media/youtube` maps `/downloads/Channel/Season 2026` to `/media/youtube/Channel/Season 2026`. This does not change Docker mounts.

Enable notifications, **Save Plex settings**, then **Test connection**. The test checks access to the selected library without starting a scan. It cannot verify Docker volume mappings; both paths must refer to the same files. The Plex path setting currently uses absolute Linux/container paths.

After confirming a completed output file, Pullarr asks Plex to scan its season folder. Plex processes accepted scan requests asynchronously. Failed notifications remain in SQLite and retry on subsequent polls/downloads, with at least 60 seconds between attempts for the same folder. They do not mark media downloads failed or download the video again. Requests for the same pending folder are combined. Settings shows pending requests and the latest result. Disabling notifications pauses retries; enabling them resumes pending requests. Notifications cover downloads completed while enabled, not existing files retroactively.

## Channel information and artwork

Pullarr fills missing `tvshow.nfo`, `poster.jpg` (the channel avatar), and `background.jpg` (the channel banner, when available) in channel folders. The NFO contains the verified YouTube channel title, description, canonical URL, and channel ID. It also creates matching episode `.nfo` files with titles, dates, season/episode numbers, and video URLs. Metadata and artwork never replace existing sidecars or modify media files.

Enabled subscriptions are checked once daily, starting on the first poll after upgrade. Existing completed downloads under the current download directory are backfilled from recorded paths; folders used before a channel title change are included. Episode NFO backfill requires the current `SYYYYEMMDD` naming. Disabled/removed subscriptions and files outside the current root are skipped. Missing YouTube images are logged; network/conversion failures retry hourly and do not stop video discovery or downloads. The retry schedule persists in a new SQLite table; existing subscriptions/history are preserved. Artwork files must be creatable on a filesystem supporting hard links for atomic, non-overwriting publication. Remove a sidecar yourself if you want Pullarr to recreate it on the next daily check.

### Prevent incorrect Plex matches

Use a dedicated YouTube **TV Shows** library with **Plex NFO Series** as its agent (Plex Media Server **1.43.1+**). This reads local channel and episode metadata instead of matching channel names to unrelated TV shows. After backfill, refresh metadata; previously locked or manually selected fields/artwork may need clearing. Pullarr requests folder scans when sidecars change, but does not change Plex library settings or existing matches itself. See [Plex’s NFO setup instructions](https://support.plex.tv/articles/using-nfo-metadata-files-with-plex/).

The ordinary Plex TV Series agent does not use these NFO files just because **Use local assets** is enabled. That option enables local artwork. On older servers, use Personal Media Shows with Local Media Assets, unmatch incorrect shows and edit titles/descriptions manually, or upgrade to use the NFO agent. NFO libraries do not support Plex’s cross-server watch-state/rating synchronization.

## YouTube episode thumbnails

New downloads save the YouTube thumbnail as a JPEG beside the video with exactly the same base filename, for example `Channel - S2026E1008 - Episode.mkv` and `Channel - S2026E1008 - Episode.jpg`. yt-dlp selects the thumbnail and converts it with ffmpeg, which is included in the Docker image. Artwork is created before Pullarr requests the Plex folder scan. Downloads and thumbnail conversion are staged in a hidden `.pullarr-partial` folder inside the download directory, then moved into place without overwriting existing media or artwork. Interrupted downloads keep their partial files for resuming; a retry may leave a staged thumbnail when final artwork already exists. Missing thumbnails are logged; a successful video download is still kept. Conversion failures use the normal download retry handling. At the end of each polling cycle, including the first cycle after restart, Pullarr removes the current download directory’s `.pullarr-partial` folder only when no download/conversion is active and the entire queue is empty. Failed/retry jobs and queued videos on disabled channels prevent cleanup so resumable downloads and conversion sources are preserved. Removed subscriptions no longer block cleanup. Completed media and sidecars outside staging are preserved; previous download directories and system temporary folders are not scanned. Cleanup errors are logged and retried on the next poll. No database migration or configuration change is required.

In Plex, edit the TV library → **Advanced** → enable **Use local assets** for the Plex TV Series agent. For legacy agents, enable and prioritize **Local Media Assets (TV)**. See [Plex’s local episode artwork guide](https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/). For an episode already indexed by Plex, refresh its metadata after adding artwork; manually selected artwork may need to be changed in the episode’s poster picker. This controls episode artwork, not timeline preview thumbnails.

Existing completed episodes are not backfilled or re-downloaded. Removing a subscription preserves media and artwork; the completed-video Remove action still deletes only the video file. No database migration or additional configuration is needed.

## Automatic yt-dlp updates

Pullarr checks for the latest stable `yt-dlp[default]` from PyPI on startup and every 24 hours, independently of polling and downloads. Failed checks are logged and retried hourly. This follows the [upstream pip update method](https://github.com/yt-dlp/yt-dlp/wiki/Installation#with-pip). No configuration is required.

Updates install into a separate temporary directory as the unprivileged app user and are activated only after a successful import/version check. Existing downloads and lookups finish with their original version; new operations use the updated version without restarting. Failed installations leave the working version intact. Updates require outbound HTTPS to PyPI and its package hosts and writable temporary storage. Temporary packages are reclaimed when no operation uses them; on restart Pullarr uses the image’s pinned version until its startup update succeeds. Subscriptions, history, media, and volume mappings are unchanged. Rebuild the image once to enable this feature. Native Python installations also need pip installed.

## Tests

`PYTHONPATH=. python -m unittest discover -s tests -v`

Handle lookups use yt-dlp channel metadata, with a fallback to the channel page. Direct channel IDs and URLs use RSS when available and fall back to channel metadata when RSS is unavailable or lacks a title. Adding a channel requires a verified channel ID and title; it does not require a working RSS feed. RSS is the primary source for ongoing discovery. If it fails, yt-dlp lists the 15 most recent uploads and Pullarr verifies their publication times from video metadata. This fallback works with every download selection, preserves the future-only cutoff, and reuses previously saved dates. Videos with unavailable or imprecise dates are retried rather than treated as new. The initial **All** scan still reads the complete upload history. Discovery still requires YouTube to allow access to either RSS or the fallback metadata.

## Fixing channels added by older releases

v0.2.1 could accidentally resolve a handle to an unrelated channel ID. If you have a channel that displays only a `UC...` ID and shows RSS 404 errors, use **Remove** in the channel list and add its handle again. This removes only the subscription, **not** downloaded media. Existing valid subscriptions remain intact. v0.2.3 can add a verified channel even when its RSS feed is unavailable. Responses that are not a channel Atom feed are rejected rather than treated as an empty feed.
