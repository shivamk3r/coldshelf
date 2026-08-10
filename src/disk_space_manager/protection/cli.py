"""Click commands for encrypted, independently replicated protected folders."""

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

import click

from disk_space_manager.utils import format_size

from .service import ProtectionError, ProtectionService


def _service() -> ProtectionService:
    return ProtectionService()


def _mutation_service() -> ProtectionService:
    service = _service()
    service.reconcile_interrupted_file_operations()
    return service


def _render(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, default=str, sort_keys=True))


def _progress(ratio: float, message: str) -> None:
    click.echo(f"[{max(0, min(int(ratio * 100), 100)):3d}%] {message}")


def _raise_click_error(exc: Exception) -> None:
    raise click.ClickException(str(exc)) from exc


def _recorded_operation(
    service: ProtectionService,
    folder: str,
    kind: str,
    request: Dict[str, Any],
    action: Callable[[str, Callable[[float, str], None]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run a CLI operation with the same durable history used by the web app."""
    folder_info = service.repository.get_folder(folder)
    operation_id = str(uuid.uuid4())
    service.repository.create_operation(
        folder_info["id"],
        kind,
        request,
        drive_id=request.get("drive_target_id"),
        operation_id=operation_id,
    )
    service.repository.start_operation(operation_id)

    def progress(ratio: float, message: str) -> None:
        service.repository.update_operation_progress(operation_id, ratio * 100, message)
        _progress(ratio, message)

    try:
        result = action(operation_id, progress)
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc) or "Operation interrupted"
        service.repository.fail_operation(
            operation_id,
            message,
            cancelled=isinstance(exc, KeyboardInterrupt) or "cancel" in message.casefold(),
        )
        raise
    service.repository.finish_operation(operation_id, result)
    return result


@click.group("protect")
def protect() -> None:
    """Protect folders in independent encrypted Restic repositories."""


@protect.command("create")
@click.option("--name", required=True, help="Stable display name for this protected folder.")
@click.option(
    "--path",
    "source_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--required-copies", default=2, show_default=True, type=click.IntRange(1, 10))
def create(name: str, source_path: Path, required_copies: int) -> None:
    """Create a protected folder and confirm its one-time recovery key."""
    try:
        service = _service()
        folder = service.create_folder(name, source_path, required_copies)
        click.echo("\nSave this recovery key in your password manager. It is shown once:\n")
        click.echo(folder["recovery_key"])
        click.echo("\nStandalone recovery instructions:\n")
        click.echo(folder["recovery_instructions"])
        pasted = click.prompt("\nPaste the recovery key to confirm", hide_input=True)
        confirmed = service.confirm_recovery_key(folder["id"], pasted)
        click.echo(f"Recovery confirmed for {confirmed['name']} ({confirmed['id']}).")
    except (ProtectionError, KeyError, OSError) as exc:
        _raise_click_error(exc)


@protect.command("confirm-recovery")
@click.argument("folder")
def confirm_recovery(folder: str) -> None:
    """Confirm a recovery key after an interrupted create flow."""
    recovery_key = click.prompt("Recovery key", hide_input=True)
    try:
        confirmed = _service().confirm_recovery_key(folder, recovery_key)
        click.echo(f"Recovery confirmed for {confirmed['name']}.")
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("volumes")
def volumes() -> None:
    """List writable mounted external volumes and stable UUIDs."""
    try:
        _render(_service().list_mounted_volumes())
    except ProtectionError as exc:
        _raise_click_error(exc)


@protect.command("enroll")
@click.argument("folder")
@click.option(
    "--mount-path",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def enroll(folder: str, mount_path: Path) -> None:
    """Initialize and enroll one independent repository on a mounted disk."""
    try:
        drive = _service().enroll_drive(folder, mount_path)
        click.echo(
            f"Enrolled {drive['volume_name']} as {drive['id']} "
            f"(repository {drive['repository_id']})."
        )
    except (ProtectionError, KeyError, OSError) as exc:
        _raise_click_error(exc)


@protect.command("status")
@click.argument("folder", required=False)
def status(folder: Optional[str]) -> None:
    """Show offline-capable protection health and the file catalog."""
    try:
        service = _service()
        if folder:
            _render(service.folder_detail(folder))
            return
        summaries = []
        for item in service.repository.list_folders():
            detail = service.folder_detail(item["id"], limit=25)
            summaries.append(
                {
                    "folder": detail["folder"],
                    "summary": detail["summary"],
                    "drives": detail["drives"],
                }
            )
        _render(summaries)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("index")
@click.argument("folder")
def index(folder: str) -> None:
    """Index local metadata without a separate content-hashing pass."""
    try:
        service = _mutation_service()
        result = _recorded_operation(
            service,
            folder,
            "index",
            {"kind": "index"},
            lambda _operation_id, progress: service.index_folder(folder, progress),
        )
        click.echo(
            f"Indexed {result['file_count']} files ({format_size(result['total_bytes'])}); "
            f"generation {result['generation']['id']}."
        )
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("backup")
@click.argument("folder")
@click.option("--drive-id", required=True)
def backup(folder: str, drive_id: str) -> None:
    """Back up the current indexed generation to one connected drive."""
    try:
        service = _mutation_service()
        result = _recorded_operation(
            service,
            folder,
            "backup",
            {"kind": "backup", "drive_target_id": drive_id},
            lambda operation_id, progress: service.backup(
                folder,
                drive_id,
                operation_id=operation_id,
                progress=progress,
            ),
        )
        _render(result)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("verify")
@click.argument("folder")
@click.option("--drive-id", required=True)
@click.option("--full", is_flag=True, help="Read repository data blocks back bit-for-bit.")
@click.option("--file-id", "file_ids", multiple=True, help="Target full verification to files.")
def verify(folder: str, drive_id: str, full: bool, file_ids: Sequence[str]) -> None:
    """Run structural or optional full verification on a connected drive."""
    if file_ids and not full:
        raise click.UsageError("--file-id is only valid with --full")
    try:
        service = _service()
        result = _recorded_operation(
            service,
            folder,
            "verify",
            {
                "kind": "verify",
                "drive_target_id": drive_id,
                "full": full,
                "file_ids": list(file_ids),
            },
            lambda operation_id, progress: service.verify(
                folder,
                drive_id,
                full=full,
                file_ids=file_ids,
                operation_id=operation_id,
                progress=progress,
            ),
        )
        _render(result)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("evict")
@click.argument("folder")
@click.option("--all", "all_files", is_flag=True, help="Select all locally present files.")
@click.option("--file-id", "file_ids", multiple=True)
@click.pass_context
def evict(ctx: click.Context, folder: str, all_files: bool, file_ids: Sequence[str]) -> None:
    """Preview, confirm, and remove files only after required verified copies."""
    if all_files == bool(file_ids):
        raise click.UsageError("Choose exactly one of --all or one or more --file-id values")
    try:
        service = _mutation_service()
        preview_request = {
            "kind": "evict",
            "file_ids": list(file_ids),
            "all_files": all_files,
            "dry_run": True,
        }
        preview = _recorded_operation(
            service,
            folder,
            "evict",
            preview_request,
            lambda operation_id, progress: service.evict(
                folder,
                file_ids=file_ids,
                all_files=all_files,
                dry_run=True,
                operation_id=operation_id,
                progress=progress,
            ),
        )
        click.echo(
            f"Eligible: {preview['eligible_count']} files "
            f"({format_size(preview['eligible_bytes'])}); blocked: {len(preview['blocked'])}."
        )
        click.echo(f"WARNING: {preview['verification_warning']}")
        if preview["blocked"]:
            _render({"blocked": preview["blocked"]})
        if ctx.find_root().obj.get("dry_run", False):
            _render({**preview, "dry_run": True})
            return
        if not preview["eligible"] or preview["blocked"]:
            raise ProtectionError("The selected set is not fully eligible for eviction")
        phrase = click.prompt(
            f"Type exactly '{preview['confirmation_phrase']}' to evict",
            default="",
            show_default=False,
        )
        result = _recorded_operation(
            service,
            folder,
            "evict",
            {
                **preview_request,
                "dry_run": False,
                "confirmation_phrase": phrase,
            },
            lambda operation_id, progress: service.evict(
                folder,
                file_ids=file_ids,
                all_files=all_files,
                dry_run=False,
                confirmation_phrase=phrase,
                operation_id=operation_id,
                progress=progress,
            ),
        )
        _render(result)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("restore")
@click.argument("folder")
@click.option("--file-id", "file_ids", multiple=True, required=True)
@click.option("--drive-id", help="Force restoration from one connected enrolled drive.")
@click.option("--destination", type=click.Path(path_type=Path))
def restore(
    folder: str,
    file_ids: Sequence[str],
    drive_id: Optional[str],
    destination: Optional[Path],
) -> None:
    """Restore selected catalog files without overwriting existing paths."""
    try:
        service = _mutation_service()
        result = _recorded_operation(
            service,
            folder,
            "restore",
            {
                "kind": "restore",
                "file_ids": list(file_ids),
                "drive_target_id": drive_id,
                "destination": str(destination) if destination else None,
            },
            lambda operation_id, progress: service.restore(
                folder,
                file_ids=file_ids,
                drive_id=drive_id,
                destination=destination,
                operation_id=operation_id,
                progress=progress,
            ),
        )
        _render(result)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)


@protect.command("import")
@click.option("--name", default="Recovered protected folder", show_default=True)
@click.option("--source-path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--mount-path",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--required-copies", default=2, show_default=True, type=click.IntRange(1, 10))
@click.option(
    "--set-id",
    "protected_folder_id",
    help="Protected set UUID when the selected disk contains more than one set.",
)
def import_(
    name: str,
    source_path: Path,
    mount_path: Path,
    required_copies: int,
    protected_folder_id: Optional[str],
) -> None:
    """Rebuild a fresh-Mac catalog from either enrolled disk."""
    recovery_key = click.prompt("Recovery key", hide_input=True)
    try:
        result = _service().import_from_drive(
            name=name,
            source_path=source_path,
            mount_path=mount_path,
            recovery_key=recovery_key,
            required_copies=required_copies,
            protected_folder_id=protected_folder_id,
        )
        _render(result)
    except (ProtectionError, KeyError) as exc:
        _raise_click_error(exc)
