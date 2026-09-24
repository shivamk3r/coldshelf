"""FastAPI application factory for coldshelf Web."""

import asyncio
import json
import platform as platform_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from coldshelf.config import DEFAULT_AGE_THRESHOLD_MONTHS
from coldshelf.directory_sizer import DirectorySizingError
from coldshelf.docker_storage import DockerConflict, DockerStorage, DockerUnavailable
from coldshelf.protection.repository import (
    DEFAULT_PROTECTION_DB_PATH,
    ProtectionRepository,
)
from coldshelf.protection.restic import MIN_RESTIC_VERSION
from coldshelf.protection.service import ProtectionError, ProtectionService

from .io_queue import HeavyIOQueue
from .browse_jobs import BrowseJobManager, BrowseJobNotFound, BrowseResultNotReady
from .deletion_jobs import (
    BrowseDeletionConflict,
    BrowseDeletionManager,
    BrowseDeletionNotFound,
    BrowseDeletionPolicy,
    DELETE_DELAY_SECONDS,
    PREVIEW_TTL_SECONDS,
)
from .docker_jobs import DockerActionManager
from .jobs import JobManager
from .protection_jobs import ProtectionOperationManager
from .repository import DEFAULT_DB_PATH, WebRepository
from .schemas import (
    ActionRequest,
    ActionResponse,
    ActionsListResponse,
    ArchiveTargetsResponse,
    ActiveBrowseDeletionResponse,
    BrowseDeletionPreviewRequest,
    BrowseDeletionPreviewResponse,
    BrowseDeletionResponse,
    BrowseDeletionScheduleRequest,
    BrowseJobResponse,
    BrowseResultResponse,
    BrowseScanCreateRequest,
    BrowseScanCreateResponse,
    DirectoryListResponse,
    JobCreateRequest,
    JobResponse,
    JobsListResponse,
    DriveEnrollmentRequest,
    DriveTarget,
    FileVersion,
    ProtectedFolder,
    ProtectedFolderCreateRequest,
    ProtectedFolderCreated,
    ProtectedFolderDeleteRequest,
    ProtectedFolderUpdateRequest,
    ProtectionOperation,
    ProtectionOperationRequest,
    ProtectionPreflightResponse,
    RecoveryConfirmationRequest,
    RecoveryImportRequest,
    StorageSummaryResponse,
    DockerActionCreateRequest,
    DockerActionResponse,
    DockerItemsResponse,
    DockerPreviewRequest,
    DockerPreviewResponse,
    DockerUsageResponse,
)
from .security import make_token_dependency
from .services import (
    ActionService,
    ServiceError,
    available_archive_targets,
    browse_directories,
    filesystem_roots,
    primary_storage_summary,
)


STATIC_DIR = Path(__file__).with_name("static")


def create_app(
    repository: Optional[WebRepository] = None,
    token: str = "",
    static_dir: Optional[Path] = None,
    dev_origin: Optional[str] = None,
    protection_repository: Optional[ProtectionRepository] = None,
    protection_service: Optional[ProtectionService] = None,
    filesystem_roots_provider: Optional[
        Callable[[], List[Dict[str, Any]]]
    ] = None,
    browse_default_path: Optional[Path] = None,
    browse_delete_delay_seconds: float = DELETE_DELAY_SECONDS,
    browse_delete_preview_ttl_seconds: float = PREVIEW_TTL_SECONDS,
    docker_storage: Optional[DockerStorage] = None,
    docker_action_delay_seconds: float = 10,
) -> FastAPI:
    """Create the FastAPI app with injected persistence and token config."""
    repo = repository or WebRepository(DEFAULT_DB_PATH)
    if protection_repository is not None:
        protection_repo = protection_repository
    elif protection_service is not None:
        protection_repo = protection_service.repository
    elif repository is not None:
        # App factories used by tests get an isolated sibling database. The server
        # injects the fixed user catalog explicitly, independent of its report DB.
        protection_repo = ProtectionRepository(
            repo.db_path.with_name(f"{repo.db_path.stem}-protected.sqlite3")
        )
    else:
        protection_repo = ProtectionRepository(DEFAULT_PROTECTION_DB_PATH)
    protection = protection_service or ProtectionService(repository=protection_repo)
    roots_provider = filesystem_roots_provider or filesystem_roots
    io_queue = HeavyIOQueue()
    job_manager = JobManager(repo, io_queue)
    deletion_policy = BrowseDeletionPolicy(
        protection_repo,
        state_paths=(repo.db_path, protection_repo.db_path),
    )
    browse_manager = BrowseJobManager(
        io_queue,
        roots_provider,
        default_path=browse_default_path,
        deletion_annotator=deletion_policy.annotate_entries,
    )
    deletion_manager = BrowseDeletionManager(
        repo,
        browse_manager,
        protection_repo,
        io_queue,
        roots_provider,
        deletion_policy,
        delete_delay_seconds=browse_delete_delay_seconds,
        preview_ttl_seconds=browse_delete_preview_ttl_seconds,
    )
    docker = docker_storage or DockerStorage()
    docker_manager = DockerActionManager(
        repo, docker, io_queue, delay_seconds=docker_action_delay_seconds
    )
    protection_manager = ProtectionOperationManager(
        protection_repo, protection, io_queue
    )
    action_service = ActionService(repo, protection_repository=protection_repo)
    verify_token = make_token_dependency(token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        deletion_manager.start()
        docker_manager.start()
        try:
            yield
        finally:
            docker_manager.shutdown()
            deletion_manager.shutdown()
            io_queue.shutdown()

    app = FastAPI(title="coldshelf Web", lifespan=lifespan)
    app.state.repository = repo
    app.state.job_manager = job_manager
    app.state.browse_manager = browse_manager
    app.state.deletion_manager = deletion_manager
    app.state.docker_manager = docker_manager
    app.state.protection_repository = protection_repo
    app.state.protection_service = protection
    app.state.protection_manager = protection_manager

    if dev_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[dev_origin],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/config", dependencies=[Depends(verify_token)])
    def config() -> dict:
        return {
            "default_age_months": DEFAULT_AGE_THRESHOLD_MONTHS,
            "duplicate_detection": True,
            "near_duplicate_detection": True,
        }

    @app.get(
        "/api/filesystem/directories",
        response_model=DirectoryListResponse,
        dependencies=[Depends(verify_token)],
    )
    def filesystem_directories(
        path: Optional[str] = None, show_hidden: bool = False
    ) -> dict:
        try:
            listing = browse_directories(path, show_hidden=show_hidden)
            listing["roots"] = roots_provider()
            return listing
        except ServiceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post(
        "/api/browse/scans",
        response_model=BrowseScanCreateResponse,
        dependencies=[Depends(verify_token)],
    )
    def create_browse_scan(request: BrowseScanCreateRequest) -> dict:
        try:
            return browse_manager.submit(request.path)
        except DirectorySizingError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/browse/scans/{scan_id}",
        response_model=BrowseJobResponse,
        dependencies=[Depends(verify_token)],
    )
    def get_browse_scan(scan_id: str) -> dict:
        try:
            return browse_manager.get_job(scan_id)
        except BrowseJobNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Browse scan not found")

    @app.get(
        "/api/browse/scans/{scan_id}/result",
        response_model=BrowseResultResponse,
        dependencies=[Depends(verify_token)],
    )
    def get_browse_result(scan_id: str) -> dict:
        try:
            return browse_manager.get_result(scan_id)
        except BrowseJobNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Browse scan not found")
        except BrowseResultNotReady as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    @app.get("/api/browse/scans/{scan_id}/events", dependencies=[Depends(verify_token)])
    async def browse_scan_events(scan_id: str) -> StreamingResponse:
        try:
            browse_manager.get_job(scan_id)
        except BrowseJobNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Browse scan not found")

        async def stream():
            while True:
                try:
                    scan = browse_manager.get_job(scan_id)
                except BrowseJobNotFound:
                    break
                yield f"event: progress\ndata: {json.dumps(scan)}\n\n"
                if scan["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post(
        "/api/browse/scans/{scan_id}/cancel",
        response_model=BrowseJobResponse,
        dependencies=[Depends(verify_token)],
    )
    def cancel_browse_scan(scan_id: str) -> dict:
        try:
            return browse_manager.cancel(scan_id)
        except BrowseJobNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Browse scan not found")

    @app.post(
        "/api/browse/deletion-previews",
        response_model=BrowseDeletionPreviewResponse,
        dependencies=[Depends(verify_token)],
    )
    def create_browse_deletion_preview(
        request: BrowseDeletionPreviewRequest,
    ) -> dict:
        try:
            return deletion_manager.create_preview(request.scan_id, request.paths)
        except BrowseJobNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Browse scan not found",
            )
        except BrowseResultNotReady as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        except BrowseDeletionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )

    @app.post(
        "/api/browse/deletions",
        response_model=BrowseDeletionResponse,
        dependencies=[Depends(verify_token)],
    )
    def schedule_browse_deletion(
        request: BrowseDeletionScheduleRequest,
    ) -> dict:
        try:
            return deletion_manager.schedule(request.preview_id)
        except BrowseDeletionNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except BrowseDeletionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )

    @app.get(
        "/api/browse/deletions/active",
        response_model=ActiveBrowseDeletionResponse,
        dependencies=[Depends(verify_token)],
    )
    def active_browse_deletion() -> dict:
        return {"deletion": deletion_manager.active()}

    @app.get(
        "/api/browse/deletions/{deletion_id}",
        response_model=BrowseDeletionResponse,
        dependencies=[Depends(verify_token)],
    )
    def get_browse_deletion(deletion_id: str) -> dict:
        try:
            return deletion_manager.get(deletion_id)
        except BrowseDeletionNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )

    @app.post(
        "/api/browse/deletions/{deletion_id}/undo",
        response_model=BrowseDeletionResponse,
        dependencies=[Depends(verify_token)],
    )
    def undo_browse_deletion(deletion_id: str) -> dict:
        try:
            return deletion_manager.undo(deletion_id)
        except BrowseDeletionNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except BrowseDeletionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )

    @app.get(
        "/api/archive-targets",
        response_model=ArchiveTargetsResponse,
        dependencies=[Depends(verify_token)],
    )
    def archive_targets() -> dict:
        return available_archive_targets()

    @app.get(
        "/api/storage",
        response_model=StorageSummaryResponse,
        dependencies=[Depends(verify_token)],
    )
    def storage_summary() -> dict:
        try:
            return primary_storage_summary()
        except ServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            )

    @app.get("/api/docker/usage", response_model=DockerUsageResponse,
             dependencies=[Depends(verify_token)])
    def docker_usage(refresh: bool = False) -> dict:
        try:
            return docker.public_usage(refresh=refresh)
        except (DockerUnavailable, DockerConflict) as exc:
            return {"available": False, "message": str(exc), "groups": {}}

    @app.get("/api/docker/items", response_model=DockerItemsResponse,
             dependencies=[Depends(verify_token)])
    def docker_items(kind: str, page: int = Query(default=1, ge=1),
                     limit: int = Query(default=50, ge=1, le=100),
                     search: str = Query(default="", max_length=200)) -> dict:
        try:
            return docker.list_items(kind, page, limit, search)
        except (DockerUnavailable, DockerConflict) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post("/api/docker/previews", response_model=DockerPreviewResponse,
              dependencies=[Depends(verify_token)])
    def docker_preview(request: DockerPreviewRequest) -> dict:
        try:
            return docker_manager.create_preview(request.kind, request.ids, request.older_than_days,
                                                 request.all_eligible)
        except (DockerUnavailable, DockerConflict) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post("/api/docker/actions", response_model=DockerActionResponse,
              dependencies=[Depends(verify_token)])
    def docker_schedule(request: DockerActionCreateRequest) -> dict:
        try:
            return docker_manager.schedule(request.preview_id, request.confirmation_phrase)
        except DockerConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    @app.get("/api/docker/actions/active", dependencies=[Depends(verify_token)])
    def active_docker_action() -> dict:
        return {"action": docker_manager.active()}

    @app.get("/api/docker/actions/recent", dependencies=[Depends(verify_token)])
    def recent_docker_actions() -> dict:
        return {"actions": docker_manager.recent()}

    @app.get("/api/docker/actions/{action_id}", response_model=DockerActionResponse,
             dependencies=[Depends(verify_token)])
    def get_docker_action(action_id: str) -> dict:
        try:
            return docker_manager.get(action_id)
        except DockerConflict as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    @app.post("/api/docker/actions/{action_id}/undo", response_model=DockerActionResponse,
              dependencies=[Depends(verify_token)])
    def undo_docker_action(action_id: str) -> dict:
        try:
            return docker_manager.undo(action_id)
        except DockerConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    @app.post(
        "/api/jobs",
        response_model=JobResponse,
        dependencies=[Depends(verify_token)],
    )
    def create_job(request: JobCreateRequest) -> dict:
        try:
            return job_manager.submit_report(request)
        except ServiceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/jobs",
        response_model=JobsListResponse,
        dependencies=[Depends(verify_token)],
    )
    def list_jobs() -> dict:
        return {"jobs": repo.list_jobs()}

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobResponse,
        dependencies=[Depends(verify_token)],
    )
    def get_job(job_id: str) -> dict:
        try:
            return repo.get_job(job_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(verify_token)])
    async def job_events(job_id: str) -> StreamingResponse:
        try:
            repo.get_job(job_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

        async def stream():
            while True:
                job = repo.get_job(job_id)
                payload = json.dumps(job)
                yield f"event: progress\ndata: {payload}\n\n"
                if job["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/jobs/{job_id}/report", dependencies=[Depends(verify_token)])
    def get_report(job_id: str) -> dict:
        try:
            report = repo.get_report(job_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
        report["cache_candidates"] = repo.list_candidates(job_id, "cache")
        report["old_candidates"] = repo.list_candidates(job_id, "old")
        report["job"] = repo.get_job(job_id)
        return report

    @app.get(
        "/api/jobs/{job_id}/actions",
        response_model=ActionsListResponse,
        dependencies=[Depends(verify_token)],
    )
    def job_actions(job_id: str) -> dict:
        try:
            repo.get_job(job_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        return {"actions": repo.list_actions(job_id)}

    @app.post(
        "/api/jobs/{job_id}/actions/clean",
        response_model=ActionResponse,
        dependencies=[Depends(verify_token)],
    )
    def clean(job_id: str, request: ActionRequest) -> dict:
        try:
            return action_service.clean(job_id, request)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        except ServiceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post(
        "/api/jobs/{job_id}/actions/archive",
        response_model=ActionResponse,
        dependencies=[Depends(verify_token)],
    )
    def archive(job_id: str, request: ActionRequest) -> dict:
        try:
            return action_service.archive(job_id, request)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        except ServiceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get("/api/protection/volumes", dependencies=[Depends(verify_token)])
    def mounted_volumes() -> dict:
        try:
            return {"volumes": protection.list_mounted_volumes()}
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/protection/preflight",
        response_model=ProtectionPreflightResponse,
        dependencies=[Depends(verify_token)],
    )
    def protection_preflight() -> dict:
        system = platform_module.system()
        minimum = ".".join(str(part) for part in MIN_RESTIC_VERSION)
        if system != "Darwin":
            return {
                "platform": system,
                "supported": False,
                "ready": False,
                "minimum_restic_version": minimum,
                "restic_version": None,
                "message": "Protected folders currently require macOS.",
            }
        try:
            payload = protection.preflight()
        except ProtectionError as exc:
            return {
                "platform": system,
                "supported": True,
                "ready": False,
                "minimum_restic_version": minimum,
                "restic_version": None,
                "message": str(exc),
            }
        return {
            "platform": system,
            "supported": True,
            "ready": True,
            "minimum_restic_version": minimum,
            "restic_version": str(payload.get("version") or "unknown"),
            "message": "Protected folders are ready to use.",
        }

    @app.get(
        "/api/protection/folders",
        response_model=list[ProtectedFolder],
        dependencies=[Depends(verify_token)],
    )
    def list_protected_folders() -> list:
        return protection_repo.list_folders()

    @app.post(
        "/api/protection/folders",
        response_model=ProtectedFolderCreated,
        dependencies=[Depends(verify_token)],
    )
    def create_protected_folder(request: ProtectedFolderCreateRequest) -> dict:
        try:
            return protection.create_folder(
                request.name, request.source_path, request.required_copies
            )
        except (ProtectionError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/protection/folders/{folder_id}",
        dependencies=[Depends(verify_token)],
    )
    def get_protected_folder(
        folder_id: str,
        query: str = "",
        limit: int = Query(default=500, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        try:
            return protection.folder_detail(
                folder_id, query=query, limit=limit, offset=offset
            )
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")

    @app.patch(
        "/api/protection/folders/{folder_id}",
        response_model=ProtectedFolder,
        dependencies=[Depends(verify_token)],
    )
    def update_protected_folder(
        folder_id: str, request: ProtectedFolderUpdateRequest
    ) -> dict:
        try:
            return protection.update_folder(
                folder_id,
                name=request.name,
                required_copies=request.required_copies,
                confirmation_phrase=request.confirmation_phrase,
            )
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.delete(
        "/api/protection/folders/{folder_id}",
        dependencies=[Depends(verify_token)],
    )
    def delete_protected_folder(
        folder_id: str, request: ProtectedFolderDeleteRequest
    ) -> dict:
        try:
            return protection.delete_folder_catalog(
                folder_id, request.confirmation_phrase
            )
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/protection/folders/{folder_id}/files",
        response_model=list[FileVersion],
        dependencies=[Depends(verify_token)],
    )
    def protected_catalog(
        folder_id: str, query: str = "", limit: int = 500, offset: int = 0
    ) -> list:
        try:
            protection_repo.get_folder(folder_id)
            return protection_repo.list_files(
                folder_id, query=query, limit=limit, offset=offset
            )
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")

    @app.post(
        "/api/protection/folders/{folder_id}/confirm-recovery",
        response_model=ProtectedFolder,
        dependencies=[Depends(verify_token)],
    )
    def confirm_recovery(folder_id: str, request: RecoveryConfirmationRequest) -> dict:
        try:
            return protection.confirm_recovery_key(folder_id, request.recovery_key)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post(
        "/api/protection/folders/{folder_id}/drives",
        response_model=DriveTarget,
        dependencies=[Depends(verify_token)],
    )
    def enroll_drive(folder_id: str, request: DriveEnrollmentRequest) -> dict:
        try:
            return protection.enroll_drive(folder_id, request.mount_path)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.post(
        "/api/protection/folders/{folder_id}/operations",
        response_model=ProtectionOperation,
        dependencies=[Depends(verify_token)],
    )
    def create_protection_operation(
        folder_id: str, request: ProtectionOperationRequest
    ) -> dict:
        try:
            protection_repo.get_folder(folder_id)
            return protection_manager.submit(folder_id, request)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protected folder not found")

    @app.get(
        "/api/protection/operations/{operation_id}",
        response_model=ProtectionOperation,
        dependencies=[Depends(verify_token)],
    )
    def get_protection_operation(operation_id: str) -> dict:
        try:
            return protection_repo.get_operation(operation_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")

    @app.get(
        "/api/protection/operations/{operation_id}/events",
        dependencies=[Depends(verify_token)],
    )
    async def protection_operation_events(operation_id: str) -> StreamingResponse:
        try:
            protection_repo.get_operation(operation_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")

        async def stream():
            while True:
                operation = protection_repo.get_operation(operation_id)
                yield f"event: progress\ndata: {json.dumps(operation)}\n\n"
                if operation["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post(
        "/api/protection/operations/{operation_id}/cancel",
        response_model=ProtectionOperation,
        dependencies=[Depends(verify_token)],
    )
    def cancel_protection_operation(operation_id: str) -> dict:
        try:
            return protection_manager.cancel(operation_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")

    @app.post(
        "/api/protection/import",
        dependencies=[Depends(verify_token)],
    )
    def import_protected_folder(request: RecoveryImportRequest) -> dict:
        try:
            return protection.import_from_drive(
                name=request.name,
                source_path=request.source_path,
                mount_path=request.mount_path,
                recovery_key=request.recovery_key,
                required_copies=request.required_copies,
                protected_folder_id=request.protected_folder_id,
            )
        except ProtectionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    resolved_static_dir = static_dir or STATIC_DIR
    if (resolved_static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(resolved_static_dir), html=True), name="static")

    return app
