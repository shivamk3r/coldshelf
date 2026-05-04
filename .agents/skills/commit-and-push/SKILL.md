---
name: commit-and-push
description: Inspect local changes, run appropriate verification, commit intentionally, and push the current branch.
---

# Commit and Push

Use this skill when the user asks to prepare, commit, and push local changes.

1. Inspect the repository state with `git status --short` and identify the
   current branch.
2. Review diffs before staging. Use focused commands such as `git diff` and
   `git diff -- <path>` so unrelated changes are not accidentally included.
3. Run the relevant lightweight verification for the repository, such as tests,
   lint, type checks, or docs checks. If the correct command is unclear, inspect
   project files and existing documentation.
4. Check that no secrets, credentials, private logs, generated data, or local
   machine paths are being committed.
5. Stage explicit paths only. Avoid broad staging unless the full change set is
   intentional and reviewed.
6. Commit using the repository's existing commit-message style. Keep the message
   clear and scoped to the staged changes.
7. Push the same branch to its upstream. If no upstream exists, push the current
   branch and set upstream intentionally.
8. Report the branch, commit hash, verification run, and any unresolved risks.
