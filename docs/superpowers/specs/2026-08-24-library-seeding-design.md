# Seeding from the library

Design for a single script, triggered by Sonarr and Radarr after import, that
repoints a completed torrent at the imported library files and stops keeping a
second copy in `/downloads`.

Date: 2026-08-24

## Problem

Every imported file exists twice on the Storage Box.

`copyUsingHardlinks` is `False` in both Sonarr and Radarr, and it cannot be
turned on: the library and `/downloads` are the same rclone FUSE mount, and
rclone's VFS does not implement `link()`. So import is a copy, and the torrent
keeps seeding its own copy until a share limit deletes it.

At the time of writing that is **336 GB of seeding data** duplicating content
already in the library, on a Storage Box that hit 95.4%.

The current mitigation is to delete aggressively — ratio 0.75, 18h seeding.
That buys space by giving up seeding.

## What makes this possible

Three facts, verified on 2026-08-24:

- **`renameEpisodes` is `False`** in Sonarr and Radarr. Imported files keep
  their original torrent filenames. Only the directory differs.
- **Non-video payload is negligible.** Across the twelve largest seeding
  torrents, everything that is not `.mkv`/`.mp4` totals **3 MB** — two Office
  packs carrying subtitles. Nothing needs deselecting; those few files simply
  re-download into the library folder.
- **qBittorrent can rename a torrent's root folder** via
  `/api/v2/torrents/renameFolder`, so a pack's root can be mapped onto Sonarr's
  `Season NN` directory.

Together these mean a completed torrent can be repointed at the library copy
with its infohash and swarm intact.

## The rule that makes it safe

**A repointed torrent must never auto-delete.**

Its files *are* the library. With `max_ratio_act = 2` (delete torrent + files),
a repointed torrent reaching its ratio limit would delete the user's media.

This is not a mitigation to remember; it is the central invariant:

- Repointed torrents get `ratio_limit = -1`, `seeding_time_limit = -1`,
  `inactive_seeding_time_limit = -1` — never auto-delete.
- They are tagged `library-seed`, so the state is visible and queryable.
- Torrents that have *not* been repointed keep the global limits, which stay as
  they are.

Seeding indefinitely costs nothing, because the bytes are the library's bytes.

## Trigger

A **Custom Script** connection in Sonarr and Radarr, on the `On Import` and
`On Upgrade` events. One script, one entry point, invoked by the libraries after
the files have been moved — not by qBittorrent, and not on a timer.

The environment carries what the script needs:

| Sonarr | Radarr | meaning |
|---|---|---|
| `sonarr_download_id` | `radarr_download_id` | **the infohash** |
| `sonarr_episodefile_path` | `radarr_moviefile_path` | imported file, final location |
| `sonarr_episodefile_sourcepath` | `radarr_moviefile_sourcepath` | where it came from |
| `sonarr_eventtype` | `radarr_eventtype` | `Download` / `Upgrade` / `Test` |

`*_download_id` is the linchpin: it is the torrent's infohash, so the script
never has to match on filenames.

### Season packs fire repeatedly

Sonarr raises `On Import` **once per episode file**. A 22-episode pack fires 22
times for one torrent.

The script therefore does not act on the event it receives. It treats every
event as "something about this infohash changed" and re-evaluates from scratch:

1. Look up the torrent by infohash. Absent → exit.
2. Already tagged `library-seed` → exit.
3. Compare the torrent's **video files** against the target directory. Any
   missing or size-mismatched → exit and wait for a later event.
4. Only when every video file is present does it repoint.

That makes the script idempotent and order-independent, which matters because
the last event of a pack is the only one that will do any work.

## Procedure

Target directory is the parent of the imported file — `/tv/The Office (US)/Season 06`
for Sonarr, the movie folder for Radarr.

1. **Export** the `.torrent` (`/torrents/export?hash=`), and record category,
   tags and trackers.
2. **Remove** the torrent with `deleteFiles=false`. The `/downloads` copy stays
   on disk for now; this is the rollback point.
3. **Re-add** the exported `.torrent` with `savepath` set to the *parent* of the
   target directory, `paused=true`, `skip_checking=false`, original category
   preserved, plus the `library-seed` tag.
4. **Rename the root folder** to the target directory's name, for multi-file
   torrents only (`renameFolder`). Single-file torrents skip this step — their
   filename already matches.
5. **Disable share limits** on the torrent: all three limits to `-1`.
6. **Force recheck**, then wait for `progress >= 1.0` minus the known extras
   allowance.
7. **On success**: delete the `/downloads` copy and resume the torrent.
8. **On failure**: delete the repointed torrent with `deleteFiles=false`, re-add
   the original pointing at `/downloads`, and leave the copy alone. Log loudly.

Step 8 is why step 2 keeps the files: until recheck passes, `/downloads` is the
only proven-good copy.

## Upgrades break repointed torrents

`downloadPropersAndRepacks` is `preferAndUpgrade`. When Sonarr upgrades an
episode it **replaces the file**, which destroys the data a repointed torrent is
seeding. The torrent goes to `missingFiles` and stops.

This is the failure mode most likely to accumulate silently, and it is why the
script also runs on `On Upgrade`:

- If the event is an upgrade and the infohash differs from the torrent currently
  seeding that path, the **old** torrent is removed with `deleteFiles=false`
  before the new one is repointed.
- A periodic reconciliation pass — a `--sweep` mode of the same script — finds
  `library-seed` torrents in `missingFiles` or `error` state and removes them
  with `deleteFiles=false`.

Sweep is invoked from the existing alerter schedule, not a new container.

## Where it runs

`config/scripts/library-seed.py`, mounted read-only into Sonarr and Radarr at
`/scripts/library-seed.py`, and registered as a Custom Script in each.

Both containers already have the library and `/downloads` bind-mounted, and both
run `network_mode: "service:tailscale"`, which reaches qBittorrent at
`172.28.0.10:8080` the same way Homepage does.

Credentials come from the environment already present in those containers;
`QBT_USERNAME` and `QBT_PASSWORD` are added to Sonarr's and Radarr's `environment`
blocks.

Python 3 is present in both LinuxServer images. No new container, no new image.

## Scope

In scope: Sonarr and Radarr.

Out of scope for v1: **Lidarr**. Music imports come predominantly from slskd
rather than torrents, `renameTracks` has not been verified, and album folder
structures differ enough to deserve their own pass.

## Testing

- The video-file comparison is a pure function over a torrent file list and a
  directory listing: table-driven tests for complete, partial, size-mismatch and
  empty-directory cases.
- The repoint procedure is tested against a stubbed qBittorrent API asserting
  the exact call sequence, including that `deleteFiles=false` is used on every
  removal path. A test asserts no code path ever issues `deleteFiles=true`.
- Share-limit disabling is asserted on the repointed torrent before it is
  resumed, not after.
- Rollback is tested by forcing recheck failure and asserting the original
  torrent is restored and `/downloads` is untouched.
- `--sweep` is tested against fabricated `missingFiles` and `error` states.
- No test talks to the live stack.

## Rollout

1. Ship the script with the Custom Script connection **disabled**, and run it by
   hand against one single-file torrent. Verify seeding continues.
2. Repeat by hand against one season pack — the Office S06, which has the
   subtitle payload and will exercise the extras path.
3. Enable on Radarr only. Movies are single-file and lowest risk.
4. Enable on Sonarr.
5. Backfill existing seeding torrents with a `--backfill` mode, one at a time.

Do not backfill before the trigger path has run clean for several days.

## Risks

**Deleting the library.** The invariant above is the whole defence. The test
asserting no path issues `deleteFiles=true` is not optional.

**Recheck cost.** Every repoint rechecks a torrent against files on a FUSE mount
backed by a Storage Box. A 50 GB pack means reading 50 GB through rclone. This
should be rate-limited to one repoint at a time, and it is the strongest reason
not to backfill in bulk.

**Extras pollute the library.** `RARBG.txt` and `Subs/` trees land in season
folders — about 3 MB across the current library. Cosmetic. Bazarr may see the
`.srt` files; it manages its own and should ignore them.

**Sonarr deleting an "unmapped" file.** Sonarr does not delete unknown files by
default, but any future cleanup feature pointed at these folders would now be
deleting seeded data.

**The migration.** This changes what lives where on the Storage Box. It should
land either well before, or well after, the netcup cutover — never during.
