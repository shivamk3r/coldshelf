import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FolderBrowser } from "./FolderBrowser";
import * as api from "./api";


vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, browseDirectories: vi.fn() };
});


test("navigates server directories, toggles hidden folders, and selects the current folder", async () => {
  vi.mocked(api.browseDirectories)
    .mockResolvedValueOnce({
      current_path: "/tmp/root",
      parent_path: "/tmp",
      roots: [{ name: "Home", path: "/home/demo", kind: "home", readable: true, symlink: false }],
      directories: [
        { name: "Child", path: "/tmp/root/Child", kind: "folder", readable: true, symlink: false },
        { name: "Private", path: "/tmp/root/Private", kind: "folder", readable: false, symlink: false }
      ]
    })
    .mockResolvedValueOnce({
      current_path: "/tmp/root/Child",
      parent_path: "/tmp/root",
      roots: [],
      directories: []
    })
    .mockResolvedValueOnce({
      current_path: "/tmp/root/Child",
      parent_path: "/tmp/root",
      roots: [],
      directories: [{ name: ".hidden", path: "/tmp/root/Child/.hidden", kind: "folder", readable: true, symlink: false }]
    });
  const onSelect = vi.fn();

  render(
    <FolderBrowser
      token="secret"
      initialPath="/tmp/root"
      onClose={vi.fn()}
      onSelect={onSelect}
    />
  );

  await userEvent.click(await screen.findByRole("button", { name: "Child" }));
  expect(api.browseDirectories).toHaveBeenNthCalledWith(2, "secret", "/tmp/root/Child", false);
  await userEvent.click(screen.getByLabelText("Show hidden folders"));
  await waitFor(() => expect(api.browseDirectories).toHaveBeenNthCalledWith(3, "secret", "/tmp/root/Child", true));
  expect(await screen.findByRole("button", { name: ".hidden" })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Use this folder" }));
  expect(onSelect).toHaveBeenCalledWith("/tmp/root/Child");
});
