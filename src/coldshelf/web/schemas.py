"""Pydantic models for the web API."""

from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from coldshelf.config import DEFAULT_AGE_THRESHOLD_MONTHS


class JobCreateRequest(BaseModel):
    """Request body for a report job."""

    path: Optional[str] = None
    age_months: int = Field(default=DEFAULT_AGE_THRESHOLD_MONTHS, ge=1, le=120)
    include_duplicates: bool = True
    include_near_duplicates: bool = True


class JobResponse(BaseModel):
    """Serialized job status."""

    id: str
    status: str
    path: str
    age_months: int
    include_duplicates: bool
    include_near_duplicates: bool
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    progress_phase: str
    progress_percent: float
    progress_message: str
    confirmation_phrases: Dict[str, str]


class JobsListResponse(BaseModel):
    jobs: List[JobResponse]


class ActionRequest(BaseModel):
    candidate_ids: List[str] = Field(default_factory=list)
    dry_run: bool = True
    confirmation_phrase: Optional[str] = None
    target_path: Optional[str] = None
    external_path: Optional[str] = None


class ActionResponse(BaseModel):
    id: str
    job_id: str
    action_type: str
    dry_run: bool
    target_path: Optional[str] = None
    selected_count: int
    skipped_count: int
    result: Dict[str, Any]
    created_at: str


class ActionsListResponse(BaseModel):
    actions: List[ActionResponse]


class DockerGroupUsage(BaseModel):
    count: int = Field(ge=0)
    active_count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)


class DockerUsageResponse(BaseModel):
    available: bool
    message: Optional[str] = None
    daemon_id: Optional[str] = None
    root_path: Optional[str] = None
    same_filesystem: bool = False
    measured_at: Optional[str] = None
    groups: Dict[str, DockerGroupUsage] = Field(default_factory=dict)


class DockerItemsResponse(BaseModel):
    kind: str
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    limit: int = Field(ge=1)
    items: List[Dict[str, Any]]


class DockerPreviewRequest(BaseModel):
    kind: Literal["images", "containers", "volumes", "cache"]
    ids: List[str] = Field(default_factory=list, max_length=1000)
    older_than_days: int = 30
    all_eligible: bool = False


class DockerCacheSummary(BaseModel):
    matching_count: int = Field(ge=0)
    matching_bytes: int = Field(ge=0)
    removable_count: int = Field(ge=0)
    removable_bytes: int = Field(ge=0)
    private_bytes: int = Field(ge=0)
    shared_bytes: int = Field(ge=0)
    unknown_shared_bytes: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)


class DockerPreviewResponse(BaseModel):
    id: str
    kind: str
    items: List[Dict[str, Any]]
    ids: List[str]
    cutoff: Optional[str] = None
    estimated_bytes: int = Field(ge=0)
    older_than_days: Optional[int] = None
    cache_summary: Optional[DockerCacheSummary] = None
    created_at: str
    expires_at: str
    confirmation_phrase: str


class DockerActionCreateRequest(BaseModel):
    preview_id: str
    confirmation_phrase: str


class DockerActionResponse(BaseModel):
    id: str
    status: str
    kind: str
    items: List[Dict[str, Any]]
    estimated_bytes: int = Field(ge=0)
    cache_summary: Optional[DockerCacheSummary] = None
    outcomes: List[Dict[str, Any]]
    created_at: str
    delete_after: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    server_time: str
    undo_available: bool


class DirectoryLocation(BaseModel):
    name: str
    path: str
    kind: str
    readable: bool = True
    symlink: bool = False


class DirectoryListResponse(BaseModel):
    current_path: str
    parent_path: Optional[str] = None
    roots: List[DirectoryLocation]
    directories: List[DirectoryLocation]


class BrowseScanCreateRequest(BaseModel):
    path: Optional[str] = None


class BrowseEntryResponse(BaseModel):
    name: str
    path: str
    kind: Literal["directory", "file", "symlink", "other"]
    hidden: bool
    readable: bool
    writable: bool
    symlink: bool
    mount_boundary: bool
    logical_bytes: Optional[int] = Field(default=None, ge=0)
    allocated_bytes: Optional[int] = Field(default=None, ge=0)
    allocation_fallback_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    directory_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    skipped_mount_count: int = Field(ge=0)
    size_state: Literal["pending", "complete", "partial", "unavailable", "boundary"]
    protection_eligible: bool
    protection_blocker: Optional[str] = None
    deletion_eligible: bool
    deletion_blocker: Optional[str] = None


class BrowseJobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    path: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    progress_percent: float = Field(ge=0, le=100)
    progress_message: str
    files_scanned: int = Field(ge=0)
    directories_scanned: int = Field(ge=0)
    completed_entries: int = Field(ge=0)
    total_entries: int = Field(ge=0)
    cancel_requested: bool


class BrowseListingResponse(BaseModel):
    current_path: str
    parent_path: Optional[str] = None
    roots: List[DirectoryLocation]
    entries: List[BrowseEntryResponse]


class BrowseScanCreateResponse(BaseModel):
    scan: BrowseJobResponse
    listing: BrowseListingResponse


class BrowseResultResponse(BaseModel):
    current_path: str
    parent_path: Optional[str] = None
    roots: List[DirectoryLocation]
    entries: List[BrowseEntryResponse]
    logical_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    directory_count: int = Field(ge=0)
    hidden_entry_count: int = Field(ge=0)
    hidden_logical_bytes: int = Field(ge=0)
    hidden_allocated_bytes: int = Field(ge=0)
    allocation_fallback_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    errors: List[str]
    measured_at: str


class BrowseDeletionPreviewRequest(BaseModel):
    scan_id: str = Field(min_length=1)
    paths: List[Path] = Field(min_length=1, max_length=1000)


class BrowseDeletionScheduleRequest(BaseModel):
    preview_id: str = Field(min_length=1)


class BrowseDeletionBlocker(BaseModel):
    path: str
    reason: str


class BrowseDeletionItem(BaseModel):
    path: str
    name: str
    kind: Literal["file", "directory"]
    logical_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    status: Literal["pending", "deleted", "skipped", "failed"] = "pending"
    error: Optional[str] = None


class BrowseDeletionPreviewResponse(BaseModel):
    id: str
    expires_at: str
    current_path: str
    selected_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    can_schedule: bool
    items: List[BrowseDeletionItem]
    blockers: List[BrowseDeletionBlocker]


class BrowseDeletionResponse(BaseModel):
    id: str
    status: Literal[
        "scheduled", "queued", "running", "completed", "cancelled", "failed"
    ]
    current_path: str
    selected_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    created_at: str
    delete_after: str
    server_time: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    undo_available: bool
    items: List[BrowseDeletionItem]


class ActiveBrowseDeletionResponse(BaseModel):
    deletion: Optional[BrowseDeletionResponse] = None


class ArchiveTargetInfo(BaseModel):
    name: str
    path: str
    available_space: int
    device: str = ""
    filesystem: str = ""


class ArchiveTargetsResponse(BaseModel):
    drives: List[ArchiveTargetInfo]
    recommended_path: Optional[str] = None


class StorageSummaryResponse(BaseModel):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    scan_root: str


class ProtectionPreflightResponse(BaseModel):
    platform: str
    supported: bool
    ready: bool
    minimum_restic_version: str
    restic_version: Optional[str] = None
    message: str


class ProtectedFolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_path: Path
    required_copies: int = Field(default=2, ge=1, le=10)


class ProtectedFolderUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    required_copies: int = Field(ge=1, le=10)
    confirmation_phrase: Optional[str] = None


class ProtectedFolderDeleteRequest(BaseModel):
    confirmation_phrase: str


class RecoveryConfirmationRequest(BaseModel):
    recovery_key: str = Field(min_length=1)


class DriveEnrollmentRequest(BaseModel):
    mount_path: Path


class RecoveryImportRequest(BaseModel):
    name: str = Field(default="Recovered protected folder", max_length=200)
    source_path: Path
    mount_path: Path
    recovery_key: str = Field(min_length=1)
    required_copies: int = Field(default=2, ge=1, le=10)
    protected_folder_id: Optional[str] = None


class _OperationBase(BaseModel):
    file_ids: List[str] = Field(default_factory=list)
    drive_target_id: Optional[str] = None
    full: bool = False
    all_files: bool = False
    dry_run: bool = True
    confirmation_phrase: Optional[str] = None
    destination: Optional[Path] = None


class IndexOperationRequest(_OperationBase):
    kind: Literal["index"]


class BackupOperationRequest(_OperationBase):
    kind: Literal["backup"]
    drive_target_id: str


class VerifyOperationRequest(_OperationBase):
    kind: Literal["verify"]
    drive_target_id: str

    @model_validator(mode="after")
    def validate_targeted_full(self):
        if self.file_ids and not self.full:
            raise ValueError("file_ids are only valid for full verification")
        return self


class EvictOperationRequest(_OperationBase):
    kind: Literal["evict"]

    @model_validator(mode="after")
    def validate_selection(self):
        if self.all_files == bool(self.file_ids):
            raise ValueError("choose exactly one of all_files or file_ids")
        return self


class RestoreOperationRequest(_OperationBase):
    kind: Literal["restore"]

    @model_validator(mode="after")
    def validate_files(self):
        if not self.file_ids:
            raise ValueError("select at least one file_id")
        return self


class RepairIdentityOperationRequest(RestoreOperationRequest):
    kind: Literal["repair_identity"]


ProtectionOperationRequest = Annotated[
    Union[
        IndexOperationRequest,
        BackupOperationRequest,
        VerifyOperationRequest,
        EvictOperationRequest,
        RestoreOperationRequest,
        RepairIdentityOperationRequest,
    ],
    Field(discriminator="kind"),
]


class ProtectedFolder(BaseModel):
    id: str
    name: str
    source_path: str
    required_copies: int
    recovery_confirmed: bool
    current_generation_id: Optional[str] = None
    created_at: str
    updated_at: str


class ProtectedFolderCreated(ProtectedFolder):
    recovery_key: str
    recovery_instructions: str


class DriveTarget(BaseModel):
    id: str
    protected_folder_id: str
    volume_uuid: str
    volume_name: str
    filesystem: str
    repo_relative_path: str
    repository_id: str
    total_bytes: int
    free_bytes: int
    status: str
    enrolled_at: str
    last_seen_at: Optional[str] = None
    last_backup_at: Optional[str] = None
    last_structural_check_at: Optional[str] = None
    last_full_check_at: Optional[str] = None
    mounted: Optional[bool] = None
    mount_path: Optional[str] = None


class ReplicaStatus(BaseModel):
    file_version_id: str
    drive_target_id: str
    generation_id: str
    snapshot_id: str
    status: str
    backed_up_at: str
    verified_at: Optional[str] = None
    full_verified_at: Optional[str] = None
    error: Optional[str] = None
    volume_name: Optional[str] = None
    volume_uuid: Optional[str] = None
    drive_status: Optional[str] = None


class FileVersion(BaseModel):
    id: str
    protected_folder_id: str
    relative_path: str
    file_type: str
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int
    local_state: str
    staged_path: Optional[str] = None
    is_current: int
    created_at: str
    updated_at: str
    replicas: List[ReplicaStatus] = Field(default_factory=list)
    verified_replica_count: int = 0


class ProtectionOperation(BaseModel):
    id: str
    protected_folder_id: str
    kind: str
    status: str
    drive_target_id: Optional[str] = None
    request: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    progress_percent: float
    progress_message: str
    eta_seconds: Optional[int] = None
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool
