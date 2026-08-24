export type JobStatus = "queued" | "running" | "completed" | "failed";

export interface Job {
  id: string;
  status: JobStatus;
  path: string;
  age_months: number;
  include_duplicates: boolean;
  include_near_duplicates: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  progress_phase: string;
  progress_percent: number;
  progress_message: string;
  confirmation_phrases: {
    clean: string;
    archive: string;
  };
}

export interface Candidate {
  id: string;
  kind: "cache" | "old";
  path: string;
  size: number;
  metadata: Record<string, unknown>;
}

export interface SizeItem {
  path: string;
  size: number;
  size_formatted: string;
}

export interface DuplicateGroup {
  kind: string;
  file_count: number;
  files: SizeItem[];
  total_size?: number;
  total_size_formatted?: string;
  reclaimable_size?: number;
  reclaimable_size_formatted?: string;
  reviewable_size?: number;
  reviewable_size_formatted?: string;
  reason?: string;
  confidence?: string;
}

export interface DuplicateSection {
  groups: DuplicateGroup[];
  group_count: number;
  duplicate_file_count?: number;
  reviewable_file_count?: number;
  reclaimable_size?: number;
  reclaimable_size_formatted?: string;
  reviewable_size?: number;
  reviewable_size_formatted?: string;
}

export interface ExtensionSummary {
  extension: string;
  count: number;
  size: number;
  size_formatted: string;
}

export interface Report {
  job: Job;
  job_id: string;
  path: string;
  age_months: number;
  scan: {
    total_scanned: number;
    error_count: number;
    errors: string[];
  };
  summary: {
    total_size: number;
    total_size_formatted: string;
    file_count: number;
    average_file_size: number;
    average_file_size_formatted: string;
    top_extensions: ExtensionSummary[];
  };
  largest_files: SizeItem[];
  largest_directories: SizeItem[];
  savings: {
    cache_size: number;
    cache_size_formatted: string;
    cache_file_count: number;
    old_files_size: number;
    old_files_size_formatted: string;
    old_files_count: number;
    exact_duplicate_reclaimable_size: number;
    exact_duplicate_reclaimable_size_formatted: string;
    exact_duplicate_file_count: number;
    total_savings: number;
    total_savings_formatted: string;
  };
  duplicates: {
    exact: DuplicateSection;
    near: DuplicateSection;
  };
  candidate_counts: {
    cache: number;
    old: number;
  };
  cache_candidates: Candidate[];
  old_candidates: Candidate[];
}

export interface ActionResult {
  id: string;
  job_id: string;
  action_type: string;
  dry_run: boolean;
  target_path?: string | null;
  selected_count: number;
  skipped_count: number;
  result: Record<string, unknown>;
  created_at: string;
}

export interface AppConfig {
  default_age_months: number;
  duplicate_detection: boolean;
  near_duplicate_detection: boolean;
}

export interface StorageSummary {
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
}

export interface DirectoryLocation {
  name: string;
  path: string;
  kind: "home" | "root" | "drive" | "folder" | string;
  readable: boolean;
  symlink: boolean;
}

export interface DirectoryListing {
  current_path: string;
  parent_path?: string | null;
  roots: DirectoryLocation[];
  directories: DirectoryLocation[];
}

export type BrowseScanStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type BrowseEntryKind = "directory" | "file" | "symlink" | "other";
export type BrowseSizeState = "pending" | "complete" | "partial" | "unavailable" | "boundary";

export interface BrowseScan {
  id: string;
  status: BrowseScanStatus;
  path: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  progress_percent: number;
  progress_message: string;
  files_scanned: number;
  directories_scanned: number;
  completed_entries: number;
  total_entries: number;
  cancel_requested: boolean;
}

export interface BrowseEntry {
  name: string;
  path: string;
  kind: BrowseEntryKind;
  hidden: boolean;
  readable: boolean;
  writable: boolean;
  symlink: boolean;
  mount_boundary: boolean;
  logical_bytes?: number | null;
  allocated_bytes?: number | null;
  allocation_fallback_count: number;
  file_count: number;
  directory_count: number;
  error_count: number;
  skipped_mount_count: number;
  size_state: BrowseSizeState;
  protection_eligible: boolean;
  protection_blocker?: string | null;
}

export interface BrowseListing {
  current_path: string;
  parent_path?: string | null;
  roots: DirectoryLocation[];
  entries: BrowseEntry[];
}

export interface BrowseResult extends BrowseListing {
  logical_bytes: number;
  allocated_bytes: number;
  file_count: number;
  directory_count: number;
  hidden_entry_count: number;
  hidden_logical_bytes: number;
  hidden_allocated_bytes: number;
  allocation_fallback_count: number;
  error_count: number;
  errors: string[];
  measured_at: string;
}

export interface BrowseScanCreated {
  scan: BrowseScan;
  listing: BrowseListing;
}

export interface ArchiveTarget {
  name: string;
  path: string;
  available_space: number;
  device: string;
  filesystem: string;
}

export interface ProtectionPreflight {
  platform: string;
  supported: boolean;
  ready: boolean;
  minimum_restic_version: string;
  restic_version?: string | null;
  message: string;
}

export interface ProtectedFolder {
  id: string;
  name: string;
  source_path: string;
  required_copies: number;
  recovery_confirmed: boolean;
  current_generation_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProtectedFolderCreated extends ProtectedFolder {
  recovery_key: string;
  recovery_instructions: string;
}

export interface MountedVolume {
  volume_uuid: string;
  name: string;
  mount_path: string;
  filesystem: string;
  device_identifier: string;
  total_bytes: number;
  free_bytes: number;
  writable: boolean;
  internal: boolean;
  removable: boolean;
  enrolled: boolean;
  drive_target_id?: string | null;
}

export interface DriveTarget {
  id: string;
  protected_folder_id: string;
  volume_uuid: string;
  volume_name: string;
  filesystem: string;
  repo_relative_path: string;
  repository_id: string;
  total_bytes: number;
  free_bytes: number;
  status: string;
  mounted: boolean;
  mount_path?: string | null;
  enrolled_at: string;
  last_seen_at?: string | null;
  last_backup_at?: string | null;
  last_structural_check_at?: string | null;
  last_full_check_at?: string | null;
}

export interface ReplicaStatus {
  file_version_id: string;
  drive_target_id: string;
  generation_id: string;
  snapshot_id: string;
  status: string;
  backed_up_at: string;
  verified_at?: string | null;
  full_verified_at?: string | null;
  error?: string | null;
  volume_name?: string;
  drive_status?: string;
}

export interface FileVersion {
  id: string;
  protected_folder_id: string;
  relative_path: string;
  file_type: string;
  size: number;
  local_state: string;
  verified_replica_count: number;
  replicas: ReplicaStatus[];
}

export interface ProtectionSummary {
  file_count: number;
  total_bytes: number;
  local_files: number;
  local_bytes: number;
  pending_files: number;
  pending_bytes: number;
  unprotected_files: number;
  unprotected_bytes: number;
  partial_files: number;
  partial_bytes: number;
  protected_files: number;
  protected_bytes: number;
  evicted_files: number;
  evicted_bytes: number;
  at_risk_files: number;
  at_risk_bytes: number;
}

export type ProtectionOperationStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ProtectionOperation {
  id: string;
  protected_folder_id: string;
  kind: "index" | "backup" | "verify" | "evict" | "restore";
  status: ProtectionOperationStatus;
  drive_target_id?: string | null;
  request: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  progress_percent: number;
  progress_message: string;
  eta_seconds?: number | null;
  diagnostics: Record<string, unknown>;
  cancel_requested: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ProtectedFolderDetail {
  folder: ProtectedFolder;
  summary: ProtectionSummary;
  drives: DriveTarget[];
  files: FileVersion[];
  operations: ProtectionOperation[];
  catalog?: {
    query: string;
    limit: number;
    offset: number;
    total: number;
  };
}
