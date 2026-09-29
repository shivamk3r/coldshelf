"""Read-only storage views and explicitly confirmed Docker cleanup commands."""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from rich.table import Table

from .directory_sizer import DirectorySizer, DirectorySizingError
from .docker_storage import DockerConflict, DockerStorage, DockerUnavailable
from .executor import ActionExecutor
from .ui import console
from .utils import format_size, home_filesystem_scan_root


@click.command("storage")
@click.option("--scan-root", is_flag=True, help="Measure readable folders from this filesystem's root")
def storage_command(scan_root: bool) -> None:
    """Show filesystem capacity and where a scan can account for space."""
    usage = shutil.disk_usage(Path.home())
    console.print(f"Filesystem containing Home: {format_size(usage.used)} used of "
                  f"{format_size(usage.total)}; {format_size(usage.free)} available")
    if not scan_root:
        console.print("Use --scan-root to measure readable top-level folders; totals may be partial.")
        return
    try:
        sizer = DirectorySizer()
        result = sizer.scan(sizer.prepare(str(home_filesystem_scan_root())))
    except DirectorySizingError as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title="Top-level folders (same filesystem)")
    table.add_column("Folder")
    table.add_column("On disk", justify="right")
    table.add_column("Status")
    for item in sorted(result["entries"], key=lambda row: row.get("allocated_bytes") or 0, reverse=True):
        if item["kind"] == "directory":
            table.add_row(item["path"], format_size(item.get("allocated_bytes") or 0), item["size_state"])
    console.print(table)
    measured = int(result["allocated_bytes"])
    console.print(f"Measured: {format_size(measured)}; scan errors: {result['error_count']}.")
    if measured <= usage.used:
        console.print(f"Not accounted for by this scan: {format_size(usage.used - measured)} "
                      "(permissions, Docker, filesystem metadata, and other causes may overlap).")
    else:
        console.print("Scan sizes exceed filesystem used; these measurements are not directly additive.")


@click.group("docker")
def docker_command() -> None:
    """Inspect and carefully clean a local Docker daemon."""


@docker_command.command("usage")
def docker_usage() -> None:
    """Show Docker usage and reclaimable estimates."""
    try:
        snapshot = DockerStorage().public_usage(refresh=True)
    except (DockerUnavailable, DockerConflict) as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title="Docker storage (categories can overlap)")
    table.add_column("Category")
    table.add_column("Items", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Reclaimable estimate", justify="right")
    for kind, group in snapshot["groups"].items():
        table.add_row(kind.title(), str(group["count"]), format_size(group["size_bytes"]),
                      format_size(group["reclaimable_bytes"]))
    console.print(table)
    console.print("Docker figures overlap shared data and must not be added to folder totals.")
    console.print("Build-cache reclaimable covers all ages and excludes image-shared cache. "
                  "An age-limited cleanup can free less because newer cache retains its parents.")


@docker_command.command("list")
@click.argument("kind", type=click.Choice(["images", "containers", "volumes"]))
@click.option("--page", type=click.IntRange(min=1), default=1)
def docker_list(kind: str, page: int) -> None:
    """List Docker items, 50 per page, largest first."""
    try:
        listing = DockerStorage().list_items(kind, page)
    except (DockerUnavailable, DockerConflict) as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title=f"Docker {kind}, page {page}")
    table.add_column("ID or name")
    table.add_column("Description")
    table.add_column("Size", justify="right")
    table.add_column("Eligible")
    for item in listing["items"]:
        table.add_row(item["id"], item["name"], format_size(item["size_bytes"]),
                      "yes" if item["eligible"] else "no")
    console.print(table)
    console.print(f"Showing {len(listing['items'])} of {listing['total']} items.")


@docker_command.command("clean")
@click.argument("kind", type=click.Choice(["images", "containers", "volumes", "cache"]))
@click.option("--id", "ids", multiple=True, help="Exact image/container ID or volume name; repeatable")
@click.option("--all-eligible", is_flag=True, help="Select all eligible images or stopped containers")
@click.option("--older-than-days", type=click.Choice(["7", "30", "90"]), default="30")
@click.pass_context
def docker_clean(ctx: click.Context, kind: str, ids: tuple[str, ...], all_eligible: bool,
                 older_than_days: str) -> None:
    """Preview and confirm one Docker cleanup category."""
    dry_run = bool(ctx.find_root().obj.get("dry_run", False))
    if kind == "cache":
        if ids or all_eligible:
            raise click.UsageError("Build cache uses --older-than-days, not item IDs.")
    elif all_eligible:
        if kind == "volumes" or ids:
            raise click.UsageError("Volumes require exact names; do not combine --all-eligible with --id.")
    elif not ids:
        raise click.UsageError("Choose --id or --all-eligible for images and containers.")
    adapter = DockerStorage()
    try:
        selected = list(ids)
        if all_eligible:
            snap = adapter.snapshot(refresh=True)
            selected = [item["id"] for item in snap["items"][kind] if item["eligible"]]
        preview = adapter.preview(kind, selected, int(older_than_days))
    except (DockerUnavailable, DockerConflict) as exc:
        raise click.ClickException(str(exc)) from exc
    count = len(preview["items"])
    if kind == "cache":
        summary = preview["cache_summary"]
        console.print(f"Preview: {count} cache records selected; unused for over {older_than_days} days.")
        console.print(f"Age-matching records: {summary['matching_count']}; "
                      f"{format_size(summary['matching_bytes'])} record size.")
        console.print(f"Retained by cache outside this selection: {summary['retained_count']} records; "
                      f"{format_size(summary['retained_bytes'])}.")
        console.print(f"Selected cache size: {format_size(summary['private_bytes'])} private; "
                      f"{format_size(summary['shared_bytes'])} shared with images.")
        if summary["unknown_shared_bytes"]:
            console.print(f"Sharing information unavailable for {format_size(summary['unknown_shared_bytes'])}.")
        console.print("Private cache size is a potential saving, not a promise of filesystem space. "
                      "Image-shared layers stay. Docker may retain records whose references change.")
    else:
        console.print(f"Preview: {count} {kind}; estimated size {format_size(preview['estimated_bytes'])}.")
    if kind == "volumes":
        console.print("Removing a volume permanently deletes its stored application data.")
    for item in preview["items"]:
        console.print(f"  {item['id']}")
    ActionExecutor(dry_run=True).log_action("DOCKER_PREVIEW", Path("docker"),
                                           details={"kind": kind, "selected_count": count,
                                                    "ids": preview["ids"], "cutoff": preview["cutoff"],
                                                    "estimated_bytes": preview["estimated_bytes"],
                                                    "cache_summary": preview.get("cache_summary")})
    if not count:
        console.print("No cache records can currently be removed within this age selection.")
        return
    if dry_run:
        return
    phrase = f"DELETE {count} DOCKER VOLUMES" if kind == "volumes" else f"REMOVE {count} DOCKER {kind.upper()}"
    if click.prompt(f"Type {phrase} to confirm", default="", show_default=False) != phrase:
        raise click.ClickException("Confirmation did not match; no Docker data was removed.")
    executor = ActionExecutor()
    try:
        with open(executor.log_file, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise click.ClickException("The action log is unavailable; no Docker data was removed.") from exc
    executor.log_action("DOCKER_SCHEDULED", Path("docker"),
                        details={"kind": kind, "selected_count": count,
                                 "ids": preview["ids"], "cutoff": preview["cutoff"]})
    def record(outcome):
        executor.log_action("DOCKER_ITEM", Path("docker"),
                            success=outcome["status"] == "deleted", error=outcome.get("error"),
                            details={"kind": kind, "outcome": outcome})
    try:
        outcomes = adapter.execute(preview, on_outcome=record)
    except (DockerUnavailable, DockerConflict) as exc:
        executor.log_action("DOCKER_CLEAN", Path("docker"), success=False, error=str(exc))
        raise click.ClickException(str(exc)) from exc
    executor.log_action("DOCKER_CLEAN", Path("docker"),
                        details={"kind": kind, "outcomes": outcomes})
    if kind == "cache":
        outcome = outcomes[0]
        console.print(f"Completed: {outcome['deleted_count']} cache records removed; "
                      f"{outcome['not_removed_count']} retained by Docker.")
        console.print(f"Docker-reported reclaimed: {format_size(outcome['reclaimed_bytes'])}. "
                      "Refresh filesystem capacity to measure the actual free-space change.")
        if outcome.get("error"):
            console.print(outcome["error"])
        return
    deleted = sum(item["status"] == "deleted" for item in outcomes)
    failed = sum(item["status"] != "deleted" for item in outcomes)
    console.print(f"Completed: {deleted} removed, {failed} skipped or failed. Run 'coldshelf docker usage' to refresh space estimates.")
