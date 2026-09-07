# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report security issues privately by emailing the maintainer, or by opening a
private advisory via GitHub's Security tab (if enabled).

When reporting, please include:

- The affected version / commit
- A description of the vulnerability
- Steps to reproduce (proof of concept, if possible)
- Impact assessment

You should receive an acknowledgement within 3 business days. We ask that you
give us reasonable time to fix and release the issue before public disclosure.

## Security Model

MoiraiCore is a **local-first** platform — it is designed to run on your own
machine and is not a hardened multi-tenant internet service. Treat it as a
trusted local application, not a public web host.

Relevant security properties:

- **Authentication**: JWT-based (HS256). Access tokens valid 24h, refresh
  tokens 7 days.
- **Password hashing**: bcrypt (cost 12), never plaintext.
- **Lockout protection**: brute-force mitigation on login (5 attempts →
  15-minute lockout).
- **Secrets**: API keys live in `.env` / `config/auth` and are **git-ignored**.
  They are never committed to the repository. Auth secret files (`users.json`,
  `auth.log`, JWT secret) are permission-restricted to `0600`.
- **Workflow/worker shell actions**: file writes are jailed to the workspace /
  project tree. Shell commands (`workflow_engine` `shell` action, worker
  `run_command`) run with cwd pinned to that tree and are refused when no
  project space is named or the target resolves under `config/`. Note:
  `shell=True` cannot be fully sandboxed — these actions are trusted-operator
  features and workflow definitions should only come from authenticated,
  authorized users.
- **Network binding**: the server binds to `127.0.0.1` only — it is not
  reachable from other machines unless you deliberately rebind or tunnel it.
- **Public endpoints**: only login/auth, the first-run setup gate (minimal
  fields), health, and first-party dashboard assets are served without a
  token. Everything else — including all mutating endpoints — requires a
  Bearer token.
- **Project isolation**: customer projects are sandboxed and reach the core
  only over HTTP; pipeline file writes are jailed to the project directory.
  Workflow `shell` actions are confined to the workspace tree, and workflow
  `save-file`/`load-file` paths are guarded against traversal.
- **Code pipeline**: all code is generated → evaluated (syntax + security
  scan) → merged. Direct writes are blocked.

## Known Limitations

- Workflow `shell` actions and worker `run_command` execute shell commands by
  design (authenticated, workspace/project-space confined). Do not share
  access tokens with parties you would not give shell access to.

## Scope

In scope: the `scripts/`, `dashboard/`, `moirai/` modules and the 3-tier
pipeline in this repository.

Out of scope: third-party dependencies, your own `.env` contents, and any
customer project spaces (which are private and not part of this repo).

## Supported Versions

Maintained on the default branch (`dev`). Security fixes are backported on a
best-effort basis to the latest release tag.
