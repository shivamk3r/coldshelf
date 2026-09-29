# Docker build-cache cleanup: diagnosis and scope

The Docker Engine 29.1.3 discrepancy involved both misleading accounting and
incomplete cleanup. Applying Docker's age filter again during a prune could
strand an otherwise removable chain after Docker refreshed a parent's last-use
timestamp. Simply relabeling the original preview would not fix that failure.

## What the numbers mean

| Figure | Meaning |
| --- | --- |
| Cache record size | BuildKit's accounting for that record's snapshots/content. It can include storage shared with an image. |
| Engine build-cache total | Sum of all reported cache record sizes. |
| Engine category reclaimable | Sum of records where both InUse and Shared are false, across all ages. It does not apply coldshelf's cutoff or its exact selection. |
| Per-record reclaimable in buildx | A record is not in use. This can be true for image-shared records and for parents retained by other idle cache records. |
| Coldshelf private cache size | Sum of known private bytes in the dependency-eligible, age-matching selection. Potential savings, not guaranteed free space. Unknown sharing is reported separately. |
| Docker-reported reclaimed | Engine's sum of sizes in the prune result, accompanied by the actual deleted IDs. It is not a filesystem capacity measurement. |
| Filesystem free-space change | Before/after free capacity on the measured filesystem, also affected by allocation, retained storage, garbage collection, and concurrent activity. Docker Desktop adds a VM/host distinction. |

These definitions explain why the reported 35 GiB selection, roughly 23 GiB
category estimate, 3.9 GiB prune accounting, and 3.1 GiB filesystem delta need
not agree. The original preview included image-shared and dependency-retained
records; the category figure also covered a different age scope. The historical
category payload was not retained, so its exact byte breakdown cannot be
reconstructed from the preview alone.

Sources: [Engine 29.1.3 DiskUsage and Prune accounting](https://github.com/moby/moby/blob/docker-v29.1.3/daemon/internal/builder-next/builder.go),
[BuildKit record sizing](https://github.com/moby/moby/blob/docker-v29.1.3/vendor/github.com/moby/buildkit/cache/refs.go),
and [Docker's buildx usage definitions](https://docs.docker.com/reference/cli/docker/buildx/du/).

## Why the records remained

BuildKit's usage report discounts references from idle children when calculating
InUse. Pruning still requires actual child references to be released. Therefore
InUse=false does not mean a parent can be removed while a newer child remains.
Prune loops over leaves and reevaluates its age filter as parents become free.
[BuildKit's usage and pruning implementation](https://github.com/moby/moby/blob/docker-v29.1.3/vendor/github.com/moby/buildkit/cache/manager.go)

In this version, loading parent references in parentsOf passes the
NoUpdateLastUsed options slice without expanding its variadic arguments. The
parent reference can consequently retain the timestamp-update behavior.
Releasing it during pruning updates last use, so the next pruning pass rejects
that parent under until. The downstream older ancestors then remain pinned.
The reference-release path and an upstream report support this diagnosis.
[Parent loading](https://github.com/moby/moby/blob/docker-v29.1.3/vendor/github.com/moby/buildkit/cache/manager.go#L523),
[reference release](https://github.com/moby/moby/blob/docker-v29.1.3/vendor/github.com/moby/buildkit/cache/refs.go#L1431),
[BuildKit issue 5436](https://github.com/moby/buildkit/issues/5436).

Coldshelf's anchored, single-value ID regex was correct. Engine translates it
into a BuildKit regex filter; multiple ID values are not accepted.
The duration filter was valid syntactically, but its repeated age check caused
the incomplete cleanup. The local fallback to creation time for never-used
records is conservative and was not the cause.
[Engine filter conversion](https://github.com/moby/moby/blob/docker-v29.1.3/daemon/internal/builder-next/builder.go#L660)

## Live evidence, 2026-09-29

The investigation used the local 29.1.3 daemon, the persisted original preview,
and relevant Docker action entries. No unrelated paths or record IDs are
included here.

- The original preview contained 959 records totaling 37,630,760,868 bytes.
  Of those, 613 still existed. Exactly 237 survivors had changed their last-use
  timestamp to the minute of the first prune, with unchanged sizes.
- The fresh seven-day selection contained 376 records totaling 30,384,296,189
  bytes (28.30 GiB). Every one had a dependency outside that fresh selection.
  Both the existing ID-plus-age prune and an ID-only prune of those same 376
  records returned zero deletions and zero bytes. Dropping age on a fresh
  preview alone cannot recover the parents already made recent by a prior run.
- Within the **original reviewed IDs**, dependency analysis predicted that 560
  of the 613 survivors could be removed; 53 were retained by genuinely newer
  cache outside the original scope.
- An explicitly authorized, logged diagnostic prune restricted to those 613
  original survivors, without until, removed exactly 560. Docker reported
  23,001,128,382 bytes (21.42 GiB). Filesystem free capacity increased by
  21,176,991,744 bytes (19.72 GiB) during the call.
- All 21 unreviewed records survived, along with their 53 retained ancestors.
  A fresh coldshelf seven-day dry-run now shows those 53 age-matching records
  as retained, with zero selected records and zero private bytes to reclaim.

The historical diagnostic was restricted using the persisted review. Normal
cleanup never imports an old action's scope or automatically retries records
that now appear recent.

## Implemented behavior and safety

Coldshelf now reads Shared, parent links, creation time, and usage count as
well as last use. It accepts Parents, the legacy Parent field, and Engine
29.1.3's actual leading-space wire key, " Parents".
[Engine cache record schema](https://github.com/moby/moby/blob/docker-v29.1.3/api/types/build/cache.go)

The preview starts with unused records older than the chosen 7/30/90-day
cutoff. A leaf-to-parent traversal selects only chains removable within that
age scope. It separately reports retained records and selected private/shared
bytes. Empty selections can be inspected but cannot be scheduled.

Immediately before execution, coldshelf verifies the same daemon, fixed cutoff,
eligible IDs, and record metadata. Changed last use, usage count, size, sharing,
or parents require a new review. One prune request uses all=true and the
anchored regex of only the reviewed IDs. It deliberately omits until so parent
timestamps changed by that same prune cannot stop the reviewed chain.
There is no broader fallback request or automatic scope expansion.

Age is checked at review and revalidation, not continuously during pruning.
Docker still guards live references. Its API provides no atomic transaction
spanning preview and prune: a reviewed record can be reused after validation,
and concurrent activity can change the result. The preview therefore never
promises either a deletion count or a filesystem byte delta.

Typed confirmation, CLI dry-run, the five-minute web preview expiry, the
ten-second cancellation window, and action logging remain. Logs now include
the reviewed IDs/cutoff, byte breakdown, and actual deleted/retained IDs and
counts. Both interfaces label the result as Docker-reported reclaimed bytes.

## Regression verification

The Docker tests use an in-memory transport; they never prune the real daemon.
The dependency fixture reproduces parent timestamp refresh under until, then
verifies that normal cleanup removes the complete reviewed chain and preserves
newer descendants. Coverage also checks all age choices, a fixed cutoff across
elapsed time, shared/unknown accounting, both API layouts, all parent wire
formats, multiple parents, reference changes, partial and zero-byte results,
empty selections, confirmation, persisted outcomes, and action logs.

Dashboard tests check the byte breakdown, all three age choices, invalidation
of a review after changing age, empty-review behavior, and actual completion
counts. Run uv run pytest, then npm test and npm run build in frontend.

Verified for this change: 208 Python tests passed (3 environment-dependent
skips), all 49 frontend tests passed, and the TypeScript/Vite production build
succeeded. The packaged dashboard was rebuilt with the updated preview and
completion messages.
