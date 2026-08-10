import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HelpCenter } from "./HelpCenter";


test("searches the in-app operation documentation", async () => {
  render(
    <HelpCenter
      onOpenReports={vi.fn()}
      onOpenProtection={vi.fn()}
    />
  );

  const search = screen.getByLabelText("Search help");
  await userEvent.type(search, "symlink");
  expect(screen.getByRole("heading", { name: "Archive old files" })).toBeInTheDocument();
  expect(screen.getByText(/original is replaced with a symlink/)).toBeInTheDocument();

  await userEvent.clear(search);
  await userEvent.type(search, "words-that-do-not-match");
  expect(screen.getByText(/No help articles match/)).toBeInTheDocument();
});


test("opens the protected-folder workflow from contextual help", async () => {
  const onOpenProtection = vi.fn();
  render(
    <HelpCenter
      initialArticle="recovery"
      onOpenReports={vi.fn()}
      onOpenProtection={onOpenProtection}
    />
  );

  expect(screen.getByRole("heading", { name: "Recover on a fresh Mac" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Open protected folders" }));
  expect(onOpenProtection).toHaveBeenCalledOnce();
});
