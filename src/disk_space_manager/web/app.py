"""FastAPI application factory for Disk Space Manager Web."""

import asyncio
import json
import platform as platform_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from disk_space_manager.config import DEFAULT_AGE_THRESHOLD_MONTHS
from disk_space_manager.protection.repository import (
    DEFAULT_PROTECTION_DB_PATH,
    ProtectionRepository,
)
from disk_space_manager.protection.restic import MIN_RESTIC_VERSION
from disk_space_manager.protection.service import ProtectionError, ProtectionService

from .io_queue import HeavyIOQueue
from .jobs import JobManager
from .protection_jobs import ProtectionOperationManager
from .repository import DEFAULT_DB_PATH, WebRepository
from .schemas import (
    ActionRequest,
    ActionResponse,
    ActionsListResponse,
    ArchiveTargetsResponse,
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
)
from .security import make_token_dependency
from .services import (
    ActionService,
    ServiceError,
    available_archive_targets,
    browse_directories,
)


STATIC_DIR = Path(__file__).with_name("static")


def create_app(
    repository: Optional[WebRepository] = None,
    token: str = "",
    static_dir: Optional[Path] = None,
    dev_origin: Optional[str] = None,
    protection_repository: Optional[ProtectionRepository] = None,
    protection_service: Optional[ProtectionService] = None,
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
    io_queue = HeavyIOQueue()
    job_manager = JobManager(repo, io_queue)
    protection_manager = ProtectionOperationManager(
        protection_repo, protection, io_queue
    )
    action_service = ActionService(repo, protection_repository=protection_repo)
    verify_token = make_token_dependency(token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        io_queue.shutdown()

    app = FastAPI(title="Disk Space Manager Web", lifespan=lifespan)
    app.state.repository = repo
    app.state.job_manager = job_manager
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
            return browse_directories(path, show_hidden=show_hidden)
        except ServiceError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @app.get(
        "/api/archive-targets",
        response_model=ArchiveTargetsResponse,
        dependencies=[Depends(verify_token)],
    )
    def archive_targets() -> dict:
        return available_archive_targets()

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
