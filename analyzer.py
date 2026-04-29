"""File analysis and categorization."""

from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable
import fnmatch

from config import (
    DEFAULT_AGE_THRESHOLD,
    CACHE_DIRECTORY_PATTERNS,
    CACHE_FILE_EXTENSIONS,
    MIN_FILE_SIZE_TO_MOVE,
)
from utils import format_size


class FileAnalyzer:
    """Analyzes files and categorizes them."""
    
    def __init__(self, age_threshold: Optional[timedelta] = None):
        """Initialize analyzer.
        
        Args:
            age_threshold: Age threshold for considering files old (default: 6 months)
        """
        self.age_threshold = age_threshold or DEFAULT_AGE_THRESHOLD
        self.now = datetime.now()
    
    def find_cache_files(self, files: List[Dict], progress_callback: Optional[Callable] = None) -> List[Dict]:
        """Identify cache files that can be safely removed."""
        cache_files = []
        total = len(files)
        
        for i, file_info in enumerate(files, 1):
            if progress_callback and i % 100 == 0:
                progress_callback(i)
            file_path = file_info['path']
            path_str = str(file_path)
            
            # Check if in cache directory
            is_cache_dir = False
            for pattern in CACHE_DIRECTORY_PATTERNS:
                if fnmatch.fnmatch(path_str, pattern):
                    is_cache_dir = True
                    break
            
            # Check if has cache extension
            is_cache_ext = file_path.suffix.lower() in CACHE_FILE_EXTENSIONS
            
            # Check common cache patterns in filename
            is_cache_name = any(
                pattern in file_path.name.lower()
                for pattern in ['cache', 'tmp', 'temp', '.log']
            )
            
            if is_cache_dir or is_cache_ext or is_cache_name:
                cache_files.append({
                    **file_info,
                    'reason': self._get_cache_reason(file_path, is_cache_dir, is_cache_ext, is_cache_name)
                })
        
        if progress_callback and total > 0:
            progress_callback(total)
        
        return cache_files
    
    def _get_cache_reason(self, path: Path, is_dir: bool, is_ext: bool, is_name: bool) -> str:
        """Get reason why file is considered cache."""
        reasons = []
        if is_dir:
            reasons.append("cache directory")
        if is_ext:
            reasons.append("cache extension")
        if is_name:
            reasons.append("cache in name")
        return ", ".join(reasons) if reasons else "cache pattern"
    
    def find_old_files(self, files: List[Dict], min_size: int = MIN_FILE_SIZE_TO_MOVE, progress_callback: Optional[Callable] = None) -> List[Dict]:
        """Find files that haven't been accessed in the threshold period."""
        old_files = []
        cutoff_date = self.now - self.age_threshold
        total = len(files)
        
        for i, file_info in enumerate(files, 1):
            if progress_callback and i % 100 == 0:
                progress_callback(i)
            # Skip if too small
            if file_info['size'] < min_size:
                continue
            
            # Check last access time
            if file_info['accessed'] < cutoff_date:
                days_old = (self.now - file_info['accessed']).days
                old_files.append({
                    **file_info,
                    'days_old': days_old,
                    'age_category': self._categorize_age(days_old)
                })
        
        if progress_callback and total > 0:
            progress_callback(total)
        
        # Sort by size (largest first)
        old_files.sort(key=lambda x: x['size'], reverse=True)
        
        return old_files
    
    def _categorize_age(self, days: int) -> str:
        """Categorize file age."""
        if days < 180:
            return "6 months"
        elif days < 365:
            return "1 year"
        elif days < 730:
            return "2 years"
        else:
            return "very old"
    
    def analyze_disk_usage(self, files: List[Dict], directories: Dict[Path, int]) -> Dict:
        """Analyze overall disk usage and provide insights."""
        total_size = sum(f['size'] for f in files)
        file_count = len(files)
        
        # Group by extension
        by_extension = {}
        for file_info in files:
            ext = file_info['path'].suffix.lower() or 'no extension'
            if ext not in by_extension:
                by_extension[ext] = {'count': 0, 'size': 0}
            by_extension[ext]['count'] += 1
            by_extension[ext]['size'] += file_info['size']
        
        # Sort extensions by size
        top_extensions = sorted(
            by_extension.items(),
            key=lambda x: x[1]['size'],
            reverse=True
        )[:10]
        
        return {
            'total_size': total_size,
            'total_size_formatted': format_size(total_size),
            'file_count': file_count,
            'top_extensions': top_extensions,
            'average_file_size': total_size / file_count if file_count > 0 else 0,
        }
    
    def calculate_potential_savings(self, cache_files: List[Dict], old_files: List[Dict]) -> Dict:
        """Calculate potential disk space savings."""
        cache_size = sum(f['size'] for f in cache_files)
        old_files_size = sum(f['size'] for f in old_files)
        total_savings = cache_size + old_files_size
        
        return {
            'cache_size': cache_size,
            'cache_size_formatted': format_size(cache_size),
            'cache_file_count': len(cache_files),
            'old_files_size': old_files_size,
            'old_files_size_formatted': format_size(old_files_size),
            'old_files_count': len(old_files),
            'total_savings': total_savings,
            'total_savings_formatted': format_size(total_savings),
        }
