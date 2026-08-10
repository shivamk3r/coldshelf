import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ReportsDashboard } from "./ReportsDashboard";
import * as api from "./api";
import type { ActionResult, Job, Report } from "./types";


const job: Job = {
  id: "job-12345678",
  status: "completed",
  path: "/tmp/source",
  age_months: 6,
  include_duplicates: true,
  include_near_duplicates: true,
  created_at: "2026-01-01T00:00:00Z",
  progress_phase: "complete",
  progress_percent: 100,
  progress_message: "Complete",
  confirmation_phrases: { clean: "CLEAN job-123", archive: "ARCHIVE job-123" }
};

const report: Report = {
  job,
  job_id: job.id,
  path: job.path,
  age_months: 6,
  scan: { total_scanned: 4, error_count: 1, errors: ["Skipped unreadable folder /tmp/private"] },
  summary: {
    total_size: 700,
    total_size_formatted: "700 B",
    file_count: 4,
    average_file_size: 175,
    average_file_size_formatted: "175 B",
    top_extensions: [{ extension: ".tmp", count: 2, size: 300, size_formatted: "300 B" }]
  },
  largest_files: [{ path: "/tmp/source/old-a.bin", size: 400, size_formatted: "400 B" }],
  largest_directories: [{ path: "/tmp/source", size: 700, size_formatted: "700 B" }],
  savings: {
    cache_size: 300,
    cache_size_formatted: "300 B",
    cache_file_count: 2,
    old_files_size: 400,
    old_files_size_formatted: "400 B",
    old_files_count: 1,
    exact_duplicate_reclaimable_size: 0,
    exact_duplicate_reclaimable_size_formatted: "0 B",
    exact_duplicate_file_count: 0,
    total_savings: 700,
    total_savings_formatted: "700 B"
  },
  duplicates: {
    exact: { groups: [], group_count: 0, reclaimable_size: 0, reclaimable_size_formatted: "0 B" },
    near: { groups: [], group_count: 0, reviewable_size: 0, reviewable_size_formatted: "0 B" }
  },
  candidate_counts: { cache: 2, old: 1 },
  cache_candidates: [
    { id: "cache-a", kind: "cache", path: "/tmp/source/cache-a.tmp", size: 100, metadata: { reason: "cache extension" } },
    { id: "cache-b", kind: "cache", path: "/tmp/source/cache-b.tmp", size: 200, metadata: { reason: "cache extension" } }
  ],
  old_candidates: [
    { id: "old-a", kind: "old", path: "/tmp/source/old-a.bin", size: 400, metadata: { days_old: 400 } }
  ]
};

function action(actionType: "clean" | "archive", dryRun: boolean, targetPath: string | null = null): ActionResult {
  return {
    id: `${actionType}-${dryRun ? "preview" : "real"}`,
    job_id: job.id,
    action_type: actionType,
    dry_run: dryRun,
    target_path: targetPath,
    selected_count: actionType === "clean" ? 2 : 1,
    skipped_count: 0,
    result: actionType === "clean" ? { deleted: 2, total_size: 300 } : { moved: 1, total_size: 400 },
    created_at: "2026-01-01T00:01:00Z"
  };
}

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getConfig: vi.fn(),
    listJobs: vi.fn(),
    getReport: vi.fn(),
    listJobActions: vi.fn(),
    listArchiveTargets: vi.fn(),
    runClean: vi.fn(),
    runArchive: vi.fn(),
    openJobEvents: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() }))
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getConfig).mockResolvedValue({ default_age_months: 6, duplicate_detection: true, near_duplicate_detection: true });
  vi.mocked(api.listJobs).mockResolvedValue({ jobs: [job] });
  vi.mocked(api.getReport).mockResolvedValue(report);
  vi.mocked(api.listJobActions).mockResolvedValue({ actions: [] });
  vi.mocked(api.listArchiveTargets).mockResolvedValue({
    drives: [{ name: "Backup", path: "/Volumes/Backup", available_space: 10_000, device: "/dev/disk9", filesystem: "ExFAT" }],
    recommended_path: "/Volumes/Backup"
  });
  vi.mocked(api.runClean).mockImplementation(async (_token, _jobId, payload) => action("clean", payload.dry_run));
  vi.mocked(api.runArchive).mockImplementation(async (_token, _jobId, payload) => action("archive", payload.dry_run, payload.external_path || payload.target_path || null));
});

test("filters candidates, invalidates changed previews, and requires the exact clean phrase", async () => {
  render(<ReportsDashboard token="secret" intent="clean" onOpenHelp={vi.fn()} onJobsChanged={vi.fn()} />);
  expect(await screen.findByRole("heading", { name: "Clean cache files" })).toBeInTheDocument();

  const search = screen.getByPlaceholderText("Filter by path");
  await userEvent.type(search, "cache-a");
  await userEvent.click(screen.getByRole("button", { name: "Select all 1 matching" }));
  await userEvent.click(screen.getByRole("button", { name: "Run dry-run preview" }));
  await waitFor(() => expect(api.runClean).toHaveBeenCalledWith("secret", job.id, {
    candidate_ids: ["cache-a"],
    dry_run: true
  }));
  expect(await screen.findByText("The current selection matches the preview.")).toBeInTheDocument();

  await userEvent.clear(search);
  await userEvent.click(screen.getByLabelText("Select /tmp/source/cache-b.tmp"));
  expect(screen.getByText("Run or refresh the preview to unlock confirmation.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Type this exact phrase")).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Run dry-run preview" }));
  const confirmation = await screen.findByLabelText("Type this exact phrase");
  const apply = screen.getByRole("button", { name: "Delete selected files" });
  expect(apply).toBeDisabled();
  await userEvent.type(confirmation, job.confirmation_phrases.clean);
  expect(apply).toBeEnabled();
  await userEvent.click(apply);
  await waitFor(() => expect(api.runClean).toHaveBeenLastCalledWith("secret", job.id, {
    candidate_ids: ["cache-a", "cache-b"],
    dry_run: false,
    confirmation_phrase: job.confirmation_phrases.clean
  }));
  expect(await screen.findByText(/report predates the completed file changes/i)).toBeInTheDocument();
});

test("sends the selected external or local archive destination in preview payloads", async () => {
  render(<ReportsDashboard token="secret" intent="archive" onOpenHelp={vi.fn()} onJobsChanged={vi.fn()} />);
  expect(await screen.findByRole("heading", { name: "Archive old files" })).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText("Select /tmp/source/old-a.bin"));
  await screen.findByRole("option", { name: /Recommended.*Backup/ });
  await userEvent.click(screen.getByRole("button", { name: "Run dry-run preview" }));
  await waitFor(() => expect(api.runArchive).toHaveBeenCalledWith("secret", job.id, {
    candidate_ids: ["old-a"],
    dry_run: true,
    external_path: "/Volumes/Backup"
  }));

  await userEvent.click(screen.getByRole("radio", { name: /Local folder/ }));
  await userEvent.type(screen.getByLabelText("Local archive folder"), "/tmp/archive");
  expect(screen.getByText("Run or refresh the preview to unlock confirmation.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Run dry-run preview" }));
  await waitFor(() => expect(api.runArchive).toHaveBeenLastCalledWith("secret", job.id, {
    candidate_ids: ["old-a"],
    dry_run: true,
    target_path: "/tmp/archive"
  }));
});
