"""Action execution with safety checks and logging."""

import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Callable

from config import ACTION_LOG_FILE
from utils import safe_delete, preserve_structure_move, format_size


class ActionExecutor:
    """Executes file operations with logging and safety checks."""
    
    def __init__(self, dry_run: bool = False, log_callback: Optional[Callable] = None):
        """Initialize executor.
        
        Args:
            dry_run: If True, don't actually perform actions
            log_callback: Optional callback for progress updates
        """
        self.dry_run = dry_run
        self.log_callback = log_callback
        self.action_log = []
        self.log_file = ACTION_LOG_FILE
    
    def log_action(self, action_type: str, source: Path, target: Optional[Path] = None, 
                   size: int = 0, success: bool = True, error: Optional[str] = None):
        """Log an action to file and memory."""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'action': action_type,
            'source': str(source),
            'target': str(target) if target else None,
            'size': size,
            'success': success,
            'error': error,
            'dry_run': self.dry_run
        }
        
        self.action_log.append(log_entry)
        
        # Write to log file
        try:
            with open(self.log_file, 'a') as f:
                f.write(f"{log_entry['timestamp']} | {action_type} | {source}")
                if target:
                    f.write(f" -> {target}")
                f.write(f" | {format_size(size)} | {'SUCCESS' if success else 'FAILED'}")
                if error:
                    f.write(f" | ERROR: {error}")
                if self.dry_run:
                    f.write(" | DRY-RUN")
                f.write("\n")
        except (IOError, PermissionError):
            pass  # Log file write failed, but continue
        
        if self.log_callback:
            self.log_callback(log_entry)
    
    def delete_files(self, files: List[Dict], confirm: bool = True) -> Dict:
        """Delete cache files.
        
        Args:
            files: List of file info dicts to delete
            confirm: Whether user confirmation is required (handled by caller)
        
        Returns:
            Dict with results: {'deleted': count, 'failed': count, 'total_size': bytes}
        """
        deleted = 0
        failed = 0
        total_size = 0
        
        for file_info in files:
            file_path = Path(file_info['path'])
            file_size = file_info['size']
            
            if self.dry_run:
                self.log_action('DELETE', file_path, size=file_size, success=True)
                deleted += 1
                total_size += file_size
                continue
            
            try:
                if safe_delete(file_path):
                    self.log_action('DELETE', file_path, size=file_size, success=True)
                    deleted += 1
                    total_size += file_size
                else:
                    self.log_action('DELETE', file_path, size=file_size, success=False, 
                                  error="Delete operation failed")
                    failed += 1
            except Exception as e:
                self.log_action('DELETE', file_path, size=file_size, success=False, 
                              error=str(e))
                failed += 1
        
        return {
            'deleted': deleted,
            'failed': failed,
            'total_size': total_size,
            'total_size_formatted': format_size(total_size)
        }
    
    def archive_files(self, files: List[Dict], target_base: Path,
                      source_base: Path, confirm: bool = True) -> Dict:
        """Archive files preserving directory structure.
        
        Args:
            files: List of file info dicts to move
            target_base: Base archive directory
            source_base: Base directory on source (to preserve structure)
            confirm: Whether user confirmation is required (handled by caller)
        
        Returns:
            Dict with results: {'moved': count, 'failed': count, 'total_size': bytes}
        """
        moved = 0
        failed = 0
        total_size = 0
        
        # Ensure target base exists
        if not self.dry_run:
            target_base.mkdir(parents=True, exist_ok=True)
        
        for file_info in files:
            source_path = Path(file_info['path'])
            file_size = file_info['size']
            target_path = preserve_structure_move(source_path, target_base, source_base)
            
            if self.dry_run:
                self.log_action('MOVE', source_path, target_path, size=file_size, success=True)
                moved += 1
                total_size += file_size
                continue
            
            try:
                # Ensure target directory exists
                target_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Copy file to target (preserve metadata)
                shutil.copy2(str(source_path), str(target_path))
                
                # Store original path for symlink
                original_path = source_path
                
                # Delete original file
                source_path.unlink()
                
                # Create symlink at original location pointing to target
                original_path.symlink_to(target_path)
                
                self.log_action('MOVE', original_path, target_path, size=file_size, success=True)
                moved += 1
                total_size += file_size
                
            except Exception as e:
                self.log_action('MOVE', source_path, target_path, size=file_size, 
                              success=False, error=str(e))
                failed += 1
        
        return {
            'moved': moved,
            'failed': failed,
            'total_size': total_size,
            'total_size_formatted': format_size(total_size)
        }
    
    def get_action_summary(self) -> Dict:
        """Get summary of all actions performed."""
        total_actions = len(self.action_log)
        successful = sum(1 for a in self.action_log if a['success'])
        failed = total_actions - successful
        total_size = sum(a['size'] for a in self.action_log)
        
        return {
            'total_actions': total_actions,
            'successful': successful,
            'failed': failed,
            'total_size': total_size,
            'total_size_formatted': format_size(total_size),
            'log_file': str(self.log_file)
        }
