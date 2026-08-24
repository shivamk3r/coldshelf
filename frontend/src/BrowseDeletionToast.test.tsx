import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  BrowseDeletionToast,
  useBrowseDeletionController
} from "./BrowseDeletionToast";
import * as api from "./api";
import type { BrowseDeletion } from "./types";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getActiveBrowseDeletion: vi.fn(),
    getBrowseDeletion: vi.fn(),
    undoBrowseDeletion: vi.fn()
  };
});

function scheduled(): BrowseDeletion {
  const created = new Date();
  const deadline = new Date(created.getTime() + 10_000);
  return {
    id: "deletion-1",
    status: "scheduled",
    current_path: "/Users/demo",
    selected_count: 1,
    logical_bytes: 20,
    allocated_bytes: 4096,
    created_at: created.toISOString(),
    delete_after: deadline.toISOString(),
    server_time: created.toISOString(),
    undo_available: true,
    items: [{
      path: "/Users/demo/notes.txt",
      name: "notes.txt",
      kind: "file",
      logical_bytes: 20,
      allocated_bytes: 4096,
      status: "pending"
    }]
  };
}

function ControllerHarness({ onFilesystemChanged = vi.fn() }: { onFilesystemChanged?: () => void }) {
  const controller = useBrowseDeletionController("secret", onFilesystemChanged);
  return <BrowseDeletionToast deletion={controller.deletion} error={controller.error} undoing={controller.undoing} onUndo={() => void controller.undo()} onDismiss={controller.dismiss} />;
}

beforeEach(() => {
  vi.clearAllMocks();
});

test("recovers an active countdown and undoes it through the backend", async () => {
  const active = scheduled();
  const cancelled = { ...active, status: "cancelled" as const, undo_available: false };
  vi.mocked(api.getActiveBrowseDeletion).mockResolvedValue({ deletion: active });
  vi.mocked(api.getBrowseDeletion).mockResolvedValue(active);
  vi.mocked(api.undoBrowseDeletion).mockResolvedValue(cancelled);

  render(<ControllerHarness />);

  expect(await screen.findByText("Permanent deletion scheduled")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => expect(api.undoBrowseDeletion).toHaveBeenCalledWith("secret", active.id));
  expect(await screen.findByText("Deletion cancelled")).toBeInTheDocument();
});

test("uses authoritative server time and refreshes Browse after completion", async () => {
  const active = {
    ...scheduled(),
    created_at: "2000-01-01T00:00:00Z",
    delete_after: "2000-01-01T00:00:10Z",
    server_time: "2000-01-01T00:00:00Z"
  };
  const completed = {
    ...active,
    status: "completed" as const,
    undo_available: false,
    items: active.items.map((item) => ({ ...item, status: "deleted" as const }))
  };
  const onFilesystemChanged = vi.fn();
  vi.mocked(api.getActiveBrowseDeletion).mockResolvedValue({ deletion: active });
  vi.mocked(api.getBrowseDeletion).mockResolvedValue(completed);

  render(<ControllerHarness onFilesystemChanged={onFilesystemChanged} />);

  expect(await screen.findByRole("button", { name: "Undo" })).toBeInTheDocument();
  expect(await screen.findByText("Deletion complete", {}, { timeout: 1500 })).toBeInTheDocument();
  expect(onFilesystemChanged).toHaveBeenCalledOnce();
});

test("reports partial completion and offers dismissal", async () => {
  const completed: BrowseDeletion = {
    ...scheduled(),
    status: "completed",
    undo_available: false,
    items: [
      { ...scheduled().items[0], status: "deleted" },
      { ...scheduled().items[0], path: "/Users/demo/blocked.txt", name: "blocked.txt", status: "skipped", error: "Item changed" }
    ],
    selected_count: 2
  };
  const onDismiss = vi.fn();
  render(<BrowseDeletionToast deletion={completed} error="" undoing={false} onUndo={vi.fn()} onDismiss={onDismiss} />);

  expect(screen.getByText("Deletion finished with warnings")).toBeInTheDocument();
  expect(screen.getByText("1 deleted; 1 skipped or failed.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Dismiss deletion notice" }));
  expect(onDismiss).toHaveBeenCalledOnce();
});
