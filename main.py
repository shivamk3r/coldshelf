#!/usr/bin/env python3
"""Main CLI entry point for Disk Space Manager."""

import os
import sys
from pathlib import Path
from datetime import timedelta
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text
from rich.tree import Tree
from rich import box

from config import DEFAULT_AGE_THRESHOLD_MONTHS, MIN_FILE_SIZE_TO_MOVE
from scanner import DiskScanner
from analyzer import FileAnalyzer
from progress_estimator import ScanProgressEstimator
from drive_detector import select_external_drive
from executor import ActionExecutor
from utils import format_size

console = Console()


class ScanAwareTimeRemainingColumn(TimeRemainingColumn):
    """Use explicit scan ETA when available, otherwise fall back to Rich."""

    def render(self, task):
        eta_text = task.fields.get("eta")
        if eta_text and not task.finished:
            return Text(str(eta_text), style="progress.remaining")
        return super().render(task)


def print_header():
    """Print application header."""
    header = """
    ╔═══════════════════════════════════════════╗
    ║        Disk Space Manager v1.0          ║
    ║   Clean, Analyze, and Archive Files     ║
    ╚═══════════════════════════════════════════╝
    """
    console.print(header, style="bold cyan")


def _is_writable_path(path: Path) -> bool:
    """Return whether a path is writable by the current process."""
    return os.access(path, os.W_OK)


def show_disk_usage_analysis(scanner: DiskScanner, analyzer: FileAnalyzer):
    """Show disk usage analysis."""
    console.print("\n[bold yellow]📊 Analyzing Disk Usage...[/bold yellow]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Scanning filesystem...", total=None)
        scan_results = scanner.scan()
        progress.update(task, completed=True)
    
    files = scan_results['files']
    directories = scan_results['directories']
    
    if scan_results['errors']:
        console.print(f"\n[yellow]⚠️  {len(scan_results['errors'])} errors encountered during scan[/yellow]")
    
    # Overall statistics
    usage_stats = analyzer.analyze_disk_usage(files, directories)
    
    console.print("\n[bold green]📈 Disk Usage Summary[/bold green]")
    stats_table = Table(box=box.ROUNDED)
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", style="green")
    
    stats_table.add_row("Total Files Scanned", f"{usage_stats['file_count']:,}")
    stats_table.add_row("Total Size", usage_stats['total_size_formatted'])
    stats_table.add_row("Average File Size", format_size(usage_stats['average_file_size']))
    
    console.print(stats_table)
    
    # Top extensions
    if usage_stats['top_extensions']:
        console.print("\n[bold green]📁 Top File Types by Size[/bold green]")
        ext_table = Table(box=box.ROUNDED)
        ext_table.add_column("Extension", style="cyan")
        ext_table.add_column("Count", style="yellow", justify="right")
        ext_table.add_column("Total Size", style="green", justify="right")
        
        for ext, data in usage_stats['top_extensions']:
            ext_table.add_row(ext, f"{data['count']:,}", format_size(data['size']))
        
        console.print(ext_table)
    
    # Largest directories
    console.print("\n[bold green]📂 Largest Directories[/bold green]")
    largest_dirs = scanner.get_largest_directories(10)
    dir_table = Table(box=box.ROUNDED)
    dir_table.add_column("Directory", style="cyan")
    dir_table.add_column("Size", style="green", justify="right")
    
    for dir_path, size in largest_dirs:
        dir_table.add_row(str(dir_path), format_size(size))
    
    console.print(dir_table)
    
    # Largest files
    console.print("\n[bold green]📄 Largest Files[/bold green]")
    largest_files = scanner.get_largest_files(10)
    file_table = Table(box=box.ROUNDED)
    file_table.add_column("File", style="cyan")
    file_table.add_column("Size", style="green", justify="right")
    
    for file_info in largest_files:
        file_table.add_row(str(file_info['path']), format_size(file_info['size']))
    
    console.print(file_table)
    
    return scan_results


def show_cache_analysis(analyzer: FileAnalyzer, files: list):
    """Show cache file analysis."""
    console.print("\n[bold yellow]🧹 Analyzing Cache Files...[/bold yellow]")
    
    cache_files = analyzer.find_cache_files(files)
    savings = analyzer.calculate_potential_savings(cache_files, [])
    
    console.print(f"\n[bold green]Found {len(cache_files)} cache files[/bold green]")
    console.print(f"Potential space savings: [green]{savings['cache_size_formatted']}[/green]")
    
    if cache_files:
        console.print("\n[bold]Sample Cache Files (first 20):[/bold]")
        cache_table = Table(box=box.ROUNDED)
        cache_table.add_column("File", style="cyan")
        cache_table.add_column("Size", style="green", justify="right")
        cache_table.add_column("Reason", style="yellow")
        
        for file_info in cache_files[:20]:
            cache_table.add_row(
                str(file_info['path']),
                format_size(file_info['size']),
                file_info.get('reason', 'cache')
            )
        
        console.print(cache_table)
    
    return cache_files


def show_old_files_analysis(analyzer: FileAnalyzer, files: list, age_months: int):
    """Show old files analysis."""
    console.print(f"\n[bold yellow]📦 Analyzing Old Files (not accessed in {age_months}+ months)...[/bold yellow]")
    
    old_files = analyzer.find_old_files(files, MIN_FILE_SIZE_TO_MOVE)
    savings = analyzer.calculate_potential_savings([], old_files)
    
    console.print(f"\n[bold green]Found {len(old_files)} old files[/bold green]")
    console.print(f"Total size: [green]{savings['old_files_size_formatted']}[/green]")
    
    if old_files:
        console.print("\n[bold]Sample Old Files (first 20):[/bold]")
        old_table = Table(box=box.ROUNDED)
        old_table.add_column("File", style="cyan")
        old_table.add_column("Size", style="green", justify="right")
        old_table.add_column("Last Accessed", style="yellow")
        old_table.add_column("Age", style="magenta")
        
        for file_info in old_files[:20]:
            days_old = file_info.get('days_old', 0)
            old_table.add_row(
                str(file_info['path']),
                format_size(file_info['size']),
                file_info['accessed'].strftime('%Y-%m-%d'),
                f"{days_old} days"
            )
        
        console.print(old_table)
    
    return old_files


@click.group()
@click.option('--dry-run', is_flag=True, help='Show what would be done without making changes')
@click.pass_context
def cli(ctx, dry_run):
    """Disk Space Manager - Clean, analyze, and archive files."""
    ctx.ensure_object(dict)
    ctx.obj['dry_run'] = dry_run
    if dry_run:
        console.print("[yellow]🔍 DRY-RUN MODE: No changes will be made[/yellow]\n")


@cli.command()
@click.option('--path', type=click.Path(exists=True, path_type=Path), 
              help='Directory to analyze (default: home directory)')
@click.pass_context
def analyze(ctx, path: Optional[Path]):
    """Analyze disk usage and show insights."""
    print_header()
    
    scan_path = path or Path.home()
    console.print(f"[cyan]Scanning: {scan_path}[/cyan]\n")
    
    scanner = DiskScanner(scan_path)
    analyzer = FileAnalyzer()
    
    show_disk_usage_analysis(scanner, analyzer)


@cli.command()
@click.option('--path', type=click.Path(exists=True, path_type=Path),
              help='Directory to scan (default: home directory)')
@click.option('--age-months', type=int, default=DEFAULT_AGE_THRESHOLD_MONTHS,
              help=f'Age threshold in months (default: {DEFAULT_AGE_THRESHOLD_MONTHS})')
@click.pass_context
def clean(ctx, path: Optional[Path], age_months: int):
    """Identify and remove cache files."""
    print_header()
    
    scan_path = path or Path.home()
    console.print(f"[cyan]Scanning: {scan_path}[/cyan]\n")
    
    scanner = DiskScanner(scan_path)
    analyzer = FileAnalyzer(age_threshold=timedelta(days=age_months * 30))
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Scanning filesystem...", total=None)
        scan_results = scanner.scan()
        progress.update(task, completed=True)
    
    files = scan_results['files']
    cache_files = show_cache_analysis(analyzer, files)
    
    if not cache_files:
        console.print("\n[green]✅ No cache files found to clean![/green]")
        return
    
    # Show summary
    savings = analyzer.calculate_potential_savings(cache_files, [])
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  • Files to delete: {len(cache_files)}")
    console.print(f"  • Space to free: {savings['cache_size_formatted']}")
    
    # Confirm before deletion
    if not ctx.obj.get('dry_run'):
        if not Confirm.ask("\n[bold red]⚠️  Delete these cache files?[/bold red]", default=False):
            console.print("[yellow]Operation cancelled.[/yellow]")
            return
    
    # Execute deletion
    executor = ActionExecutor(dry_run=ctx.obj.get('dry_run', False))
    console.print("\n[bold yellow]🗑️  Deleting cache files...[/bold yellow]")
    
    result = executor.delete_files(cache_files, confirm=False)
    
    console.print(f"\n[green]✅ Deletion complete![/green]")
    console.print(f"  • Deleted: {result['deleted']} files")
    console.print(f"  • Failed: {result['failed']} files")
    console.print(f"  • Space freed: {result['total_size_formatted']}")
    
    if executor.action_log:
        console.print(f"\n[dim]Action log: {executor.log_file}[/dim]")


@cli.command()
@click.option('--path', type=click.Path(exists=True, path_type=Path),
              help='Directory to scan (default: home directory)')
@click.option('--target-path', type=click.Path(path_type=Path),
              help='Local folder to use as archive destination')
@click.option('--external-path', type=click.Path(path_type=Path),
              help='Path to external drive (default: auto-detect)')
@click.option('--age-months', type=int, default=DEFAULT_AGE_THRESHOLD_MONTHS,
              help=f'Age threshold in months (default: {DEFAULT_AGE_THRESHOLD_MONTHS})')
@click.pass_context
def archive(ctx, path: Optional[Path], target_path: Optional[Path], external_path: Optional[Path], age_months: int):
    """Move old files to an external drive or local folder."""
    print_header()
    
    scan_path = path or Path.home()
    console.print(f"[cyan]Scanning: {scan_path}[/cyan]\n")
    
    # Determine archive target: --target-path > --external-path > auto-detect external drive
    if target_path:
        try:
            target_path.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            console.print(f"[red]❌ Error: Cannot create archive folder {target_path}: {e}[/red]")
            sys.exit(1)
        archive_target = target_path
        target_label = "local folder"
        console.print(f"[green]✅ Using local archive folder: {archive_target}[/green]")
    elif external_path:
        if not external_path.exists() or not _is_writable_path(external_path):
            console.print(f"[red]❌ Error: Path {external_path} does not exist or is not writable[/red]")
            sys.exit(1)
        archive_target = external_path
        target_label = "external drive"
        console.print(f"[green]✅ Using external drive: {archive_target}[/green]")
    else:
        console.print("[bold yellow]🔍 Detecting external drive...[/bold yellow]")
        try:
            archive_target = select_external_drive()
            if not archive_target:
                console.print("[red]❌ No external drive detected. Use --external-path or --target-path[/red]")
                sys.exit(1)
            target_label = "external drive"
            console.print(f"[green]✅ Using external drive: {archive_target}[/green]")
        except Exception as e:
            console.print(f"[red]❌ Error detecting external drive: {e}[/red]")
            sys.exit(1)
    
    archive_base = archive_target / "archived_files"
    scanner = DiskScanner(scan_path, exclude_paths=[archive_target])
    analyzer = FileAnalyzer(age_threshold=timedelta(days=age_months * 30))
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Scanning filesystem...", total=None)
        scan_results = scanner.scan()
        progress.update(task, completed=True)
    
    files = scan_results['files']
    # Skip symlinks (e.g. left behind from previous archive runs)
    files = [f for f in files if not os.path.islink(f['path'])]
    old_files = show_old_files_analysis(analyzer, files, age_months)
    
    if not old_files:
        console.print(f"\n[green]✅ No old files found (not accessed in {age_months}+ months)![/green]")
        return
    
    # Show summary
    savings = analyzer.calculate_potential_savings([], old_files)
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  • Files to move: {len(old_files)}")
    console.print(f"  • Space to archive: {savings['old_files_size_formatted']}")
    console.print(f"  • Target: {archive_target}")
    
    # Confirm before moving
    if not ctx.obj.get('dry_run'):
        if not Confirm.ask(f"\n[bold red]⚠️  Move these files to {target_label}?[/bold red]", default=False):
            console.print("[yellow]Operation cancelled.[/yellow]")
            return
    
    # Execute move
    executor = ActionExecutor(dry_run=ctx.obj.get('dry_run', False))
    console.print(f"\n[bold yellow]📦 Moving files to {target_label}...[/bold yellow]")
    result = executor.archive_files(old_files, archive_base, scan_path, confirm=False)
    
    console.print(f"\n[green]✅ Archive complete![/green]")
    console.print(f"  • Moved: {result['moved']} files")
    console.print(f"  • Failed: {result['failed']} files")
    console.print(f"  • Space archived: {result['total_size_formatted']}")
    console.print(f"  • Location: {archive_base}")
    
    if executor.action_log:
        console.print(f"\n[dim]Action log: {executor.log_file}[/dim]")


@cli.command()
@click.option('--path', type=click.Path(exists=True, path_type=Path),
              help='Directory to scan (default: home directory)')
@click.option('--age-months', type=int, default=DEFAULT_AGE_THRESHOLD_MONTHS,
              help=f'Age threshold in months (default: {DEFAULT_AGE_THRESHOLD_MONTHS})')
def full_report(path: Optional[Path], age_months: int):
    """Generate a full analysis report."""
    print_header()
    
    scan_path = path or Path.home()
    console.print(f"[cyan]Scanning: {scan_path}[/cyan]\n")
    
    scanner = DiskScanner(scan_path)
    analyzer = FileAnalyzer(age_threshold=timedelta(days=age_months * 30))
    
    # Run all analyses with progress tracking
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        ScanAwareTimeRemainingColumn(),
        console=console,
    ) as progress:
        # Phase 1: Scan filesystem with a moving heuristic estimate.
        estimator = ScanProgressEstimator()
        scan_task = progress.add_task(
            "[cyan]Phase 1/3:[/cyan] Scanning filesystem... (estimating)",
            total=estimator.placeholder_total,
            eta="ETA estimating",
        )
        
        def on_scan_progress(scan_progress):
            estimate = estimator.update(scan_progress)
            if scan_progress.is_finished:
                return
            estimate_label = (
                "estimating total"
                if estimate.is_estimating
                else f"~{estimate.total:,} est."
            )
            progress.update(
                scan_task,
                completed=estimate.completed,
                total=estimate.total,
                eta=estimate.eta_text,
                description=(
                    "[cyan]Phase 1/3:[/cyan] Scanning filesystem... "
                    f"({scan_progress.files_scanned:,} files, "
                    f"{scan_progress.directories_remaining:,} dirs left, "
                    f"{estimate_label})"
                ),
            )
        
        scanner.detailed_progress_callback = on_scan_progress
        scan_results = scanner.scan()
        files = scan_results['files']
        directories = scan_results['directories']
        total_files = len(files)
        progress.update(
            scan_task, total=total_files, completed=total_files,
            eta="",
            description=f"[green]Phase 1/3:[/green] Scan complete ({total_files:,} files found)")
        
        # Phase 2: Identify cache files
        analysis_total = max(total_files, 1)
        cache_task = progress.add_task(
            "[cyan]Phase 2/3:[/cyan] Identifying cache files...",
            total=analysis_total)
        
        cache_files = analyzer.find_cache_files(
            files,
            progress_callback=lambda n: progress.update(cache_task, completed=n))
        progress.update(
            cache_task, completed=analysis_total,
            description=f"[green]Phase 2/3:[/green] Found {len(cache_files):,} cache files")
        
        # Phase 3: Find old files
        old_task = progress.add_task(
            f"[cyan]Phase 3/3:[/cyan] Finding old files (>{age_months} months)...",
            total=analysis_total)
        
        old_files = analyzer.find_old_files(
            files, MIN_FILE_SIZE_TO_MOVE,
            progress_callback=lambda n: progress.update(old_task, completed=n))
        progress.update(
            old_task, completed=analysis_total,
            description=f"[green]Phase 3/3:[/green] Found {len(old_files):,} old files")
    
    # Compute disk usage stats (fast, no progress needed)
    usage_stats = analyzer.analyze_disk_usage(files, directories)
    
    # Display results
    if scan_results['errors']:
        console.print(f"\n[yellow]⚠️  {len(scan_results['errors'])} errors encountered during scan[/yellow]")
    
    console.print("\n[bold green]📈 Disk Usage Summary[/bold green]")
    stats_table = Table(box=box.ROUNDED)
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", style="green")
    stats_table.add_row("Total Files Scanned", f"{usage_stats['file_count']:,}")
    stats_table.add_row("Total Size", usage_stats['total_size_formatted'])
    stats_table.add_row("Average File Size", format_size(usage_stats['average_file_size']))
    console.print(stats_table)
    
    if usage_stats['top_extensions']:
        console.print("\n[bold green]📁 Top File Types by Size[/bold green]")
        ext_table = Table(box=box.ROUNDED)
        ext_table.add_column("Extension", style="cyan")
        ext_table.add_column("Count", style="yellow", justify="right")
        ext_table.add_column("Total Size", style="green", justify="right")
        for ext, data in usage_stats['top_extensions']:
            ext_table.add_row(ext, f"{data['count']:,}", format_size(data['size']))
        console.print(ext_table)
    
    console.print("\n[bold green]📂 Largest Directories[/bold green]")
    largest_dirs = scanner.get_largest_directories(10)
    dir_table = Table(box=box.ROUNDED)
    dir_table.add_column("Directory", style="cyan")
    dir_table.add_column("Size", style="green", justify="right")
    for dir_path, size in largest_dirs:
        dir_table.add_row(str(dir_path), format_size(size))
    console.print(dir_table)
    
    console.print("\n[bold green]📄 Largest Files[/bold green]")
    largest_files = scanner.get_largest_files(10)
    file_table = Table(box=box.ROUNDED)
    file_table.add_column("File", style="cyan")
    file_table.add_column("Size", style="green", justify="right")
    for file_info in largest_files:
        file_table.add_row(str(file_info['path']), format_size(file_info['size']))
    console.print(file_table)
    
    cache_savings = analyzer.calculate_potential_savings(cache_files, [])
    console.print(f"\n[bold green]🧹 Cache Files[/bold green]")
    console.print(f"Found {len(cache_files)} cache files")
    console.print(f"Potential space savings: [green]{cache_savings['cache_size_formatted']}[/green]")
    if cache_files:
        console.print("\n[bold]Sample Cache Files (first 20):[/bold]")
        cache_table = Table(box=box.ROUNDED)
        cache_table.add_column("File", style="cyan")
        cache_table.add_column("Size", style="green", justify="right")
        cache_table.add_column("Reason", style="yellow")
        for cf in cache_files[:20]:
            cache_table.add_row(str(cf['path']), format_size(cf['size']), cf.get('reason', 'cache'))
        console.print(cache_table)
    
    old_savings = analyzer.calculate_potential_savings([], old_files)
    console.print(f"\n[bold green]📦 Old Files (not accessed in {age_months}+ months)[/bold green]")
    console.print(f"Found {len(old_files)} old files")
    console.print(f"Total size: [green]{old_savings['old_files_size_formatted']}[/green]")
    if old_files:
        console.print("\n[bold]Sample Old Files (first 20):[/bold]")
        old_table = Table(box=box.ROUNDED)
        old_table.add_column("File", style="cyan")
        old_table.add_column("Size", style="green", justify="right")
        old_table.add_column("Last Accessed", style="yellow")
        old_table.add_column("Age", style="magenta")
        for of in old_files[:20]:
            days_old = of.get('days_old', 0)
            old_table.add_row(
                str(of['path']), format_size(of['size']),
                of['accessed'].strftime('%Y-%m-%d'), f"{days_old} days")
        console.print(old_table)
    
    # Overall savings potential
    savings = analyzer.calculate_potential_savings(cache_files, old_files)
    
    console.print("\n[bold green]💾 Potential Space Savings[/bold green]")
    savings_table = Table(box=box.ROUNDED)
    savings_table.add_column("Category", style="cyan")
    savings_table.add_column("Files", style="yellow", justify="right")
    savings_table.add_column("Size", style="green", justify="right")
    
    savings_table.add_row(
        "Cache Files",
        f"{savings['cache_file_count']:,}",
        savings['cache_size_formatted']
    )
    savings_table.add_row(
        "Old Files (Archive)",
        f"{savings['old_files_count']:,}",
        savings['old_files_size_formatted']
    )
    savings_table.add_row(
        "[bold]Total Potential Savings[/bold]",
        f"[bold]{savings['cache_file_count'] + savings['old_files_count']:,}[/bold]",
        f"[bold]{savings['total_savings_formatted']}[/bold]"
    )
    
    console.print(savings_table)


if __name__ == '__main__':
    cli()
