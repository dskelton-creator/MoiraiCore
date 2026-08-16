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

- **Authentication**: JWT-based. Access tokens valid 24h, refresh tokens 30d.
- **Password hashing**: PBKDF2-SHA256 (never plaintext).
- **Lockout protection**: brute-force mitigation on login.
- **Secrets**: API keys live in `.env` / `config/auth` and are **git-ignored**.
  They are never committed to the repository.
- **Project isolation**: customer projects are sandboxed and reach the core
  only over HTTP; file writes are jailed to the project directory.
- **Code pipeline**: all code is generated → evaluated (syntax + security
  scan) → merged to protected `src/`. Direct writes are blocked.

## Scope

In scope: the `scripts/`, `dashboard/`, `moirai/` modules and the 3-tier
pipeline in this repository.

Out of scope: third-party dependencies, your own `.env` contents, and any
customer project spaces (which are private and not part of this repo).

## Supported Versions

Maintained on the default branch (`dev`). Security fixes are backported on a
best-effort basis to the latest release tag.
