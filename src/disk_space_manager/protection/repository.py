"""SQLite catalog for protected folders and their independent replicas."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


DEFAULT_PROTECTION_DB_PATH = Path.home() / ".disk-space-manager-protected.sqlite3"
SCHEMA_VERSION = 2
VERIFIED_REPLICA_STATES = frozenset({"structural_checked", "full_checked"})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ProtectionRepository:
    """Persistent catalog shared by the CLI and web dashboard."""

    def __init__(self, db_path: Path = DEFAULT_PROTECTION_DB_PATH):
        self.db_path = Path(db_path).expanduser()
        self._lock = threading.RLock()
        self.initialize()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        self.db_path.chmod(0o600)
        with self._read() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, SCHEMA_VERSION}:
                raise RuntimeError(
                    f"Unsupported protected-folder database version {version}; "
                    f"expected {SCHEMA_VERSION}"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS protected_folders (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_path TEXT NOT NULL UNIQUE,
                    required_copies INTEGER NOT NULL CHECK(required_copies >= 1),
                    recovery_confirmed INTEGER NOT NULL DEFAULT 0,
                    current_generation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drive_targets (
                    id TEXT PRIMARY KEY,
                    protected_folder_id TEXT NOT NULL
                        REFERENCES protected_folders(id) ON DELETE CASCADE,
                    volume_uuid TEXT NOT NULL,
                    volume_name TEXT NOT NULL,
                    filesystem TEXT NOT NULL,
                    repo_relative_path TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    free_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'offline',
                    enrolled_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    last_backup_at TEXT,
                    last_structural_check_at TEXT,
                    last_full_check_at TEXT,
                    UNIQUE(protected_folder_id, volume_uuid),
                    UNIQUE(protected_folder_id, repository_id)
                );

                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY,
                    protected_folder_id TEXT NOT NULL
                        REFERENCES protected_folders(id) ON DELETE CASCADE,
                    manifest_digest TEXT NOT NULL,
                    file_count INTEGER NOT NULL,
                    total_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'indexed',
                    created_at TEXT NOT NULL,
                    UNIQUE(protected_folder_id, manifest_digest)
                );

                CREATE TABLE IF NOT EXISTS file_versions (
                    id TEXT PRIMARY KEY,
                    protected_folder_id TEXT NOT NULL
                        REFERENCES protected_folders(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    ctime_ns INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    device INTEGER NOT NULL,
                    local_state TEXT NOT NULL,
                    staged_path TEXT,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_file_versions_current
                    ON file_versions(protected_folder_id, relative_path)
                    WHERE is_current = 1;

                CREATE INDEX IF NOT EXISTS idx_file_versions_folder_state
                    ON file_versions(protected_folder_id, is_current, local_state);

                CREATE TABLE IF NOT EXISTS generation_entries (
                    generation_id TEXT NOT NULL
                        REFERENCES generations(id) ON DELETE CASCADE,
                    file_version_id TEXT NOT NULL
                        REFERENCES file_versions(id) ON DELETE CASCADE,
                    PRIMARY KEY(generation_id, file_version_id)
                );

                CREATE TABLE IF NOT EXISTS replicas (
                    file_version_id TEXT NOT NULL
                        REFERENCES file_versions(id) ON DELETE CASCADE,
                    drive_target_id TEXT NOT NULL
                        REFERENCES drive_targets(id) ON DELETE CASCADE,
                    generation_id TEXT NOT NULL
                        REFERENCES generations(id) ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    backed_up_at TEXT NOT NULL,
                    verified_at TEXT,
                    full_verified_at TEXT,
                    error TEXT,
                    PRIMARY KEY(file_version_id, drive_target_id)
                );

                CREATE INDEX IF NOT EXISTS idx_replicas_drive_status
                    ON replicas(drive_target_id, status);

                CREATE TABLE IF NOT EXISTS protection_operations (
                    id TEXT PRIMARY KEY,
                    protected_folder_id TEXT NOT NULL
                        REFERENCES protected_folders(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    drive_target_id TEXT,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    progress_percent REAL NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT 'Queued',
                    eta_seconds INTEGER,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_operations_folder_created
                    ON protection_operations(protected_folder_id, created_at DESC);
                """
            )
            operation_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(protection_operations)").fetchall()
            }
            if "eta_seconds" not in operation_columns:
                conn.execute(
                    "ALTER TABLE protection_operations ADD COLUMN eta_seconds INTEGER"
                )
            if "diagnostics_json" not in operation_columns:
                conn.execute(
                    """
                    ALTER TABLE protection_operations
                    ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'
                    """
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create_folder(
        self,
        folder_id: str,
        name: str,
        source_path: Path,
        required_copies: int,
        recovery_confirmed: bool = False,
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO protected_folders (
                    id, name, source_path, required_copies, recovery_confirmed,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    folder_id,
                    name,
                    str(source_path),
                    required_copies,
                    int(recovery_confirmed),
                    now,
                    now,
                ),
            )
        return self.get_folder(folder_id)

    def list_folders(self) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM protected_folders ORDER BY name COLLATE NOCASE, created_at"
            ).fetchall()
        return [self._folder_dict(row) for row in rows]

    def get_folder(self, folder_id_or_name: str) -> Dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT * FROM protected_folders
                WHERE id = ? OR name = ?
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (folder_id_or_name, folder_id_or_name, folder_id_or_name),
            ).fetchone()
        if row is None:
            raise KeyError(folder_id_or_name)
        return self._folder_dict(row)

    def set_recovery_confirmed(self, folder_id: str, confirmed: bool = True) -> None:
        with self._write() as conn:
            conn.execute(
                """
                UPDATE protected_folders
                SET recovery_confirmed = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(confirmed), utc_now(), folder_id),
            )

    def update_folder(
        self, folder_id: str, *, name: str, required_copies: int
    ) -> Dict[str, Any]:
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE protected_folders
                SET name = ?, required_copies = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, int(required_copies), utc_now(), folder_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(folder_id)
        return self.get_folder(folder_id)

    def count_evicted_files(self, folder_id: str) -> int:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM file_versions
                WHERE protected_folder_id = ? AND is_current = 1
                  AND local_state NOT IN ('local', 'restored')
                """,
                (folder_id,),
            ).fetchone()
        return int(row["count"])

    def add_drive(
        self,
        folder_id: str,
        *,
        volume_uuid: str,
        volume_name: str,
        filesystem: str,
        repo_relative_path: str,
        repository_id: str,
        total_bytes: int,
        free_bytes: int,
        drive_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        drive_id = drive_id or str(uuid.uuid4())
        now = utc_now()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO drive_targets (
                    id, protected_folder_id, volume_uuid, volume_name, filesystem,
                    repo_relative_path, repository_id, total_bytes, free_bytes,
                    status, enrolled_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?)
                """,
                (
                    drive_id,
                    folder_id,
                    volume_uuid,
                    volume_name,
                    filesystem,
                    repo_relative_path,
                    repository_id,
                    int(total_bytes),
                    int(free_bytes),
                    now,
                    now,
                ),
            )
        return self.get_drive(drive_id)

    def list_drives(self, folder_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM drive_targets"
        params: Sequence[Any] = ()
        if folder_id:
            sql += " WHERE protected_folder_id = ?"
            params = (folder_id,)
        sql += " ORDER BY enrolled_at, volume_name COLLATE NOCASE"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_drive(self, drive_id: str) -> Dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM drive_targets WHERE id = ?", (drive_id,)
            ).fetchone()
        if row is None:
            raise KeyError(drive_id)
        return dict(row)

    def delete_drive(self, drive_id: str) -> None:
        """Remove an enrollment row while leaving its on-disk repository intact."""
        with self._write() as conn:
            conn.execute("DELETE FROM drive_targets WHERE id = ?", (drive_id,))

    def update_drive_seen(
        self,
        drive_id: str,
        *,
        volume_name: str,
        total_bytes: int,
        free_bytes: int,
        status: str = "online",
    ) -> None:
        with self._write() as conn:
            conn.execute(
                """
                UPDATE drive_targets
                SET volume_name = ?, total_bytes = ?, free_bytes = ?,
                    status = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    volume_name,
                    int(total_bytes),
                    int(free_bytes),
                    status,
                    utc_now(),
                    drive_id,
                ),
            )

    def set_all_drives_offline(self) -> None:
        with self._write() as conn:
            conn.execute("UPDATE drive_targets SET status = 'offline'")

    def save_generation(
        self,
        folder_id: str,
        manifest_digest: str,
        entries: Iterable[Dict[str, Any]],
        *,
        retire_paths: Iterable[str] = (),
    ) -> Dict[str, Any]:
        entry_list = list(entries)
        retired_paths = set(retire_paths)
        now = utc_now()
        with self._write() as conn:
            generation_row = conn.execute(
                """
                SELECT * FROM generations
                WHERE protected_folder_id = ? AND manifest_digest = ?
                """,
                (folder_id, manifest_digest),
            ).fetchone()
            if generation_row is None:
                generation_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO generations (
                        id, protected_folder_id, manifest_digest, file_count,
                        total_bytes, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'indexed', ?)
                    """,
                    (
                        generation_id,
                        folder_id,
                        manifest_digest,
                        len(entry_list),
                        sum(int(entry["size"]) for entry in entry_list),
                        now,
                    ),
                )
            else:
                generation_id = generation_row["id"]

            current_rows = {
                row["relative_path"]: row
                for row in conn.execute(
                    """
                    SELECT * FROM file_versions
                    WHERE protected_folder_id = ? AND is_current = 1
                    """,
                    (folder_id,),
                ).fetchall()
            }
            seen_paths = set()
            version_ids: List[str] = []
            for entry in entry_list:
                relative_path = entry["relative_path"]
                seen_paths.add(relative_path)
                current = current_rows.get(relative_path)
                if current is not None and _same_fingerprint(current, entry):
                    version_id = current["id"]
                    conn.execute(
                        """
                        UPDATE file_versions
                        SET local_state = 'local', staged_path = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, version_id),
                    )
                else:
                    if current is not None:
                        conn.execute(
                            "UPDATE file_versions SET is_current = 0, updated_at = ? WHERE id = ?",
                            (now, current["id"]),
                        )
                    version_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO file_versions (
                            id, protected_folder_id, relative_path, file_type, size,
                            mtime_ns, ctime_ns, inode, device, local_state,
                            is_current, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', 1, ?, ?)
                        """,
                        (
                            version_id,
                            folder_id,
                            relative_path,
                            entry["file_type"],
                            int(entry["size"]),
                            int(entry["mtime_ns"]),
                            int(entry["ctime_ns"]),
                            int(entry["inode"]),
                            int(entry["device"]),
                            now,
                            now,
                        ),
                    )
                version_ids.append(version_id)

            for relative_path, current in current_rows.items():
                if relative_path in seen_paths or current["local_state"] == "evicted":
                    continue
                conn.execute(
                    """
                    UPDATE file_versions
                    SET local_state = 'missing', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, current["id"]),
                )

            retired_paths.difference_update(seen_paths)
            conn.executemany(
                """
                UPDATE file_versions
                SET is_current = 0, updated_at = ?
                WHERE protected_folder_id = ? AND relative_path = ? AND is_current = 1
                """,
                [(now, folder_id, path) for path in sorted(retired_paths)],
            )

            conn.execute(
                "DELETE FROM generation_entries WHERE generation_id = ?",
                (generation_id,),
            )
            conn.executemany(
                """
                INSERT INTO generation_entries (generation_id, file_version_id)
                VALUES (?, ?)
                """,
                [(generation_id, version_id) for version_id in version_ids],
            )
            conn.execute(
                """
                UPDATE protected_folders
                SET current_generation_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (generation_id, now, folder_id),
            )
        return self.get_generation(generation_id)

    def get_generation(self, generation_id: str) -> Dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(generation_id)
        return dict(row)

    def list_generation_entries(self, generation_id: str) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT fv.*
                FROM generation_entries ge
                JOIN file_versions fv ON fv.id = ge.file_version_id
                WHERE ge.generation_id = ?
                ORDER BY fv.relative_path
                """,
                (generation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_generation_replicated(
        self,
        generation_id: str,
        drive_id: str,
        snapshot_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        now = utc_now()
        verified_at = now if status in VERIFIED_REPLICA_STATES else None
        full_verified_at = now if status == "full_checked" else None
        with self._write() as conn:
            file_rows = conn.execute(
                "SELECT file_version_id FROM generation_entries WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            conn.executemany(
                """
                INSERT INTO replicas (
                    file_version_id, drive_target_id, generation_id, snapshot_id,
                    status, backed_up_at, verified_at, full_verified_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_version_id, drive_target_id) DO UPDATE SET
                    generation_id = excluded.generation_id,
                    snapshot_id = excluded.snapshot_id,
                    status = excluded.status,
                    backed_up_at = excluded.backed_up_at,
                    verified_at = COALESCE(excluded.verified_at, replicas.verified_at),
                    full_verified_at = COALESCE(
                        excluded.full_verified_at, replicas.full_verified_at
                    ),
                    error = excluded.error
                """,
                [
                    (
                        row["file_version_id"],
                        drive_id,
                        generation_id,
                        snapshot_id,
                        status,
                        now,
                        verified_at,
                        full_verified_at,
                        error,
                    )
                    for row in file_rows
                ],
            )
            fields = ["last_backup_at = ?", "status = 'online'"]
            values: List[Any] = [now]
            if status in VERIFIED_REPLICA_STATES:
                fields.append("last_structural_check_at = ?")
                values.append(now)
            if status == "full_checked":
                fields.append("last_full_check_at = ?")
                values.append(now)
            values.append(drive_id)
            conn.execute(
                f"UPDATE drive_targets SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def mark_drive_verified(self, drive_id: str, full: bool = False) -> None:
        now = utc_now()
        with self._write() as conn:
            if full:
                conn.execute(
                    """
                    UPDATE replicas
                    SET status = 'full_checked', verified_at = ?, full_verified_at = ?,
                        error = NULL
                    WHERE drive_target_id = ?
                    """,
                    (now, now, drive_id),
                )
                conn.execute(
                    """
                    UPDATE drive_targets
                    SET last_structural_check_at = ?, last_full_check_at = ?,
                        status = 'online'
                    WHERE id = ?
                    """,
                    (now, now, drive_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE replicas
                    SET status = CASE
                        WHEN status = 'full_checked' THEN status
                        ELSE 'structural_checked'
                    END,
                    verified_at = ?, error = NULL
                    WHERE drive_target_id = ?
                    """,
                    (now, drive_id),
                )
                conn.execute(
                    """
                    UPDATE drive_targets
                    SET last_structural_check_at = ?, status = 'online'
                    WHERE id = ?
                    """,
                    (now, drive_id),
                )

    def mark_file_replicas_full(self, drive_id: str, file_ids: Sequence[str]) -> None:
        if not file_ids:
            return
        now = utc_now()
        placeholders = ",".join("?" for _ in file_ids)
        with self._write() as conn:
            conn.execute(
                f"""
                UPDATE replicas
                SET status = 'full_checked', verified_at = ?, full_verified_at = ?,
                    error = NULL
                WHERE drive_target_id = ? AND file_version_id IN ({placeholders})
                """,
                [now, now, drive_id, *file_ids],
            )
            conn.execute(
                """
                UPDATE drive_targets
                SET status = 'online'
                WHERE id = ?
                """,
                (drive_id,),
            )

    def mark_replicas_suspect(self, drive_id: str, error: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE replicas SET status = 'suspect', error = ? WHERE drive_target_id = ?",
                (error, drive_id),
            )
            conn.execute(
                "UPDATE drive_targets SET status = 'error' WHERE id = ?", (drive_id,)
            )

    def list_files(
        self,
        folder_id: str,
        *,
        query: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM file_versions
            WHERE protected_folder_id = ? AND is_current = 1
        """
        params: List[Any] = [folder_id]
        if query:
            sql += " AND relative_path LIKE ? ESCAPE '\\'"
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        sql += " ORDER BY relative_path COLLATE NOCASE LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 2000)), max(0, offset)])
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                replica_rows = conn.execute(
                    """
                    SELECT r.*, d.volume_name, d.volume_uuid, d.status AS drive_status
                    FROM replicas r
                    JOIN drive_targets d ON d.id = r.drive_target_id
                    WHERE r.file_version_id = ?
                    ORDER BY d.enrolled_at
                    """,
                    (row["id"],),
                ).fetchall()
                item["replicas"] = [dict(replica) for replica in replica_rows]
                item["verified_replica_count"] = sum(
                    1
                    for replica in replica_rows
                    if replica["status"] in VERIFIED_REPLICA_STATES
                )
                result.append(item)
        return result

    def count_files(self, folder_id: str, *, query: str = "") -> int:
        """Count current catalog entries, optionally matching a path search."""
        sql = """
            SELECT COUNT(*) AS count FROM file_versions
            WHERE protected_folder_id = ? AND is_current = 1
        """
        params: List[Any] = [folder_id]
        if query:
            sql += " AND relative_path LIKE ? ESCAPE '\\'"
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        with self._read() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["count"] if row else 0)

    def get_file_versions(
        self, folder_id: str, file_ids: Sequence[str]
    ) -> List[Dict[str, Any]]:
        if not file_ids:
            return []
        placeholders = ",".join("?" for _ in file_ids)
        with self._read() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM file_versions
                WHERE protected_folder_id = ? AND is_current = 1
                  AND id IN ({placeholders})
                """,
                [folder_id, *file_ids],
            ).fetchall()
        return [dict(row) for row in rows]

    def get_file_replicas(self, file_id: str) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT r.*, d.volume_uuid, d.volume_name, d.status AS drive_status,
                       d.repo_relative_path, d.repository_id
                FROM replicas r
                JOIN drive_targets d ON d.id = r.drive_target_id
                WHERE r.file_version_id = ?
                ORDER BY d.enrolled_at
                """,
                (file_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_file_state(
        self,
        file_id: str,
        local_state: str,
        staged_path: Optional[Path] = None,
        fingerprint: Optional[Dict[str, int]] = None,
    ) -> None:
        fields = ["local_state = ?", "staged_path = ?", "updated_at = ?"]
        values: List[Any] = [
            local_state,
            str(staged_path) if staged_path else None,
            utc_now(),
        ]
        if fingerprint:
            for name in ("size", "mtime_ns", "ctime_ns", "inode", "device"):
                fields.append(f"{name} = ?")
                values.append(int(fingerprint[name]))
        values.append(file_id)
        with self._write() as conn:
            conn.execute(
                f"UPDATE file_versions SET {', '.join(fields)} WHERE id = ?", values
            )

    def list_incomplete_file_operations(self) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM file_versions
                WHERE local_state IN ('evicting', 'restoring')
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def fail_interrupted_operations(self) -> None:
        """Make operations left active by a prior process explicitly retryable."""
        now = utc_now()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM protection_operations WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                diagnostics = _operation_diagnostics(
                    row,
                    now,
                    {"exception_type": "ProcessInterrupted"},
                )
                conn.execute(
                    """
                    UPDATE protection_operations
                    SET status = 'failed', error = 'Interrupted; safe to retry',
                        progress_message = 'Interrupted; safe to retry',
                        eta_seconds = NULL, diagnostics_json = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(diagnostics, sort_keys=True), now, row["id"]),
                )

    def folder_summary(self, folder_id: str) -> Dict[str, Any]:
        folder = self.get_folder(folder_id)
        files = self.list_all_files(folder_id)
        summary = {
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "local_files": 0,
            "local_bytes": 0,
            "protected_files": 0,
            "protected_bytes": 0,
            "pending_files": 0,
            "pending_bytes": 0,
            "unprotected_files": 0,
            "unprotected_bytes": 0,
            "partial_files": 0,
            "partial_bytes": 0,
            "evicted_files": 0,
            "evicted_bytes": 0,
            "at_risk_files": 0,
            "at_risk_bytes": 0,
        }
        for item in files:
            local = item["local_state"] in {"local", "restored"}
            protected = item["verified_replica_count"] >= folder["required_copies"]
            if local:
                summary["local_files"] += 1
                summary["local_bytes"] += item["size"]
            if protected:
                summary["protected_files"] += 1
                summary["protected_bytes"] += item["size"]
            else:
                summary["pending_files"] += 1
                summary["pending_bytes"] += item["size"]
                if item["verified_replica_count"] == 0:
                    summary["unprotected_files"] += 1
                    summary["unprotected_bytes"] += item["size"]
                else:
                    summary["partial_files"] += 1
                    summary["partial_bytes"] += item["size"]
            if item["local_state"] == "evicted":
                summary["evicted_files"] += 1
                summary["evicted_bytes"] += item["size"]
            if not local and not protected:
                summary["at_risk_files"] += 1
                summary["at_risk_bytes"] += item["size"]
        return summary

    def list_all_files(self, folder_id: str) -> List[Dict[str, Any]]:
        """Return every current file with its verified replica count."""
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT fv.*,
                       COUNT(DISTINCT CASE
                           WHEN r.status IN ('structural_checked', 'full_checked')
                           THEN r.drive_target_id END) AS verified_replica_count
                FROM file_versions fv
                LEFT JOIN replicas r ON r.file_version_id = fv.id
                WHERE fv.protected_folder_id = ? AND fv.is_current = 1
                GROUP BY fv.id
                ORDER BY fv.relative_path COLLATE NOCASE
                """,
                (folder_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_folder(self, folder_id: str) -> None:
        """Remove a catalog row, used to roll back a failed recovery import."""
        with self._write() as conn:
            conn.execute("DELETE FROM protected_folders WHERE id = ?", (folder_id,))

    def create_operation(
        self,
        folder_id: str,
        kind: str,
        request: Dict[str, Any],
        drive_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO protection_operations (
                    id, protected_folder_id, kind, status, drive_target_id,
                    request_json, progress_percent, progress_message, created_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, 0, 'Queued', ?)
                """,
                (
                    operation_id,
                    folder_id,
                    kind,
                    drive_id,
                    json.dumps(request, sort_keys=True),
                    utc_now(),
                ),
            )
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> Dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM protection_operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _operation_dict(row)

    def list_operations(self, folder_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM protection_operations
                WHERE protected_folder_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (folder_id, limit),
            ).fetchall()
        return [_operation_dict(row) for row in rows]

    def start_operation(self, operation_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                """
                UPDATE protection_operations
                SET status = 'running', started_at = ?, progress_message = 'Starting',
                    eta_seconds = NULL
                WHERE id = ?
                """,
                (utc_now(), operation_id),
            )

    def update_operation_progress(
        self,
        operation_id: str,
        percent: float,
        message: str,
        eta_seconds: Optional[int] = None,
    ) -> None:
        normalized_eta = None if eta_seconds is None else max(0, int(eta_seconds))
        with self._write() as conn:
            conn.execute(
                """
                UPDATE protection_operations
                SET progress_percent = ?, progress_message = ?, eta_seconds = ?
                WHERE id = ?
                """,
                (
                    max(0.0, min(float(percent), 100.0)),
                    message,
                    normalized_eta,
                    operation_id,
                ),
            )

    def finish_operation(
        self,
        operation_id: str,
        result: Dict[str, Any],
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_at = utc_now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM protection_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            diagnostic_summary = _operation_diagnostics(row, finished_at, diagnostics)
            conn.execute(
                """
                UPDATE protection_operations
                SET status = 'completed', result_json = ?, progress_percent = 100,
                    progress_message = 'Complete', eta_seconds = NULL,
                    diagnostics_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(result, default=str, sort_keys=True),
                    json.dumps(diagnostic_summary, default=str, sort_keys=True),
                    finished_at,
                    operation_id,
                ),
            )

    def fail_operation(
        self,
        operation_id: str,
        error: str,
        cancelled: bool = False,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_at = utc_now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM protection_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            diagnostic_summary = _operation_diagnostics(row, finished_at, diagnostics)
            conn.execute(
                """
                UPDATE protection_operations
                SET status = ?, error = ?, progress_message = ?, eta_seconds = NULL,
                    diagnostics_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    "cancelled" if cancelled else "failed",
                    error,
                    "Cancelled" if cancelled else error,
                    json.dumps(diagnostic_summary, default=str, sort_keys=True),
                    finished_at,
                    operation_id,
                ),
            )

    def request_cancel(self, operation_id: str) -> None:
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM protection_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            diagnostics = _stored_diagnostics(row)
            diagnostics.setdefault(
                "last_progress_percent", float(row["progress_percent"])
            )
            diagnostics.setdefault(
                "last_progress_message", str(row["progress_message"])
            )
            conn.execute(
                """
                UPDATE protection_operations
                SET cancel_requested = 1, progress_message = 'Cancellation requested',
                    eta_seconds = NULL, diagnostics_json = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (json.dumps(diagnostics, sort_keys=True), operation_id),
            )

    def protected_roots(self) -> List[Path]:
        return [Path(folder["source_path"]) for folder in self.list_folders()]

    def is_protected_path(self, path: Path) -> bool:
        candidate = Path(path).expanduser().resolve(strict=False)
        for root in self.protected_roots():
            resolved_root = root.resolve(strict=False)
            try:
                candidate.relative_to(resolved_root)
                return True
            except ValueError:
                continue
        return False

    def _folder_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["recovery_confirmed"] = bool(item["recovery_confirmed"])
        return item

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()


def _same_fingerprint(row: sqlite3.Row, entry: Dict[str, Any]) -> bool:
    return all(
        row[field] == entry[field]
        for field in (
            "file_type",
            "size",
            "mtime_ns",
            "ctime_ns",
            "inode",
            "device",
        )
    )


def _operation_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json"))
    result_json = item.pop("result_json")
    item["result"] = json.loads(result_json) if result_json else None
    diagnostics_json = item.pop("diagnostics_json", None)
    item["diagnostics"] = json.loads(diagnostics_json) if diagnostics_json else {}
    item["cancel_requested"] = bool(item["cancel_requested"])
    return item


def operation_failure_diagnostics(exc: BaseException) -> Dict[str, Any]:
    """Return compact, non-secret exception metadata for an operation record."""
    diagnostics: Dict[str, Any] = {"exception_type": type(exc).__name__}
    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        exit_code = getattr(current, "exit_code", None)
        if exit_code is not None:
            diagnostics["restic_exit_code"] = int(exit_code)
            break
        current = current.__cause__ or current.__context__
    return diagnostics


def _operation_diagnostics(
    row: sqlite3.Row,
    finished_at: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    diagnostics = _stored_diagnostics(row)
    diagnostics.setdefault("last_progress_percent", float(row["progress_percent"]))
    diagnostics.setdefault("last_progress_message", str(row["progress_message"]))
    started_at = row["started_at"]
    if started_at:
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            diagnostics["duration_seconds"] = max(
                0.0, round((finished - started).total_seconds(), 3)
            )
        except ValueError:
            pass
    diagnostics.update(extra or {})
    return diagnostics


def _stored_diagnostics(row: sqlite3.Row) -> Dict[str, Any]:
    raw = row["diagnostics_json"] if "diagnostics_json" in row.keys() else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
