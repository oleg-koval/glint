# Security Policy

## Scope

glint is a read-only status-line script. It:

- reads the session JSON Claude Code pipes to it on stdin,
- reads your transcript file to compute context size,
- runs a few read-only `git` commands in the current directory.

It does **not** write to your repository or project, make network requests, or
execute anything from the session payload. The installer is the only component
that writes — it places `glint.py` in `~/.claude` and edits `~/.claude/settings.json`.

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/oleg-koval/glint/security/advisories/new)
rather than a public issue. You can expect an initial response within a few days.
