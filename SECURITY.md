# Security policy

## Reporting a vulnerability

Write to **info@synlabs.pro** with a description and, if possible, steps to
reproduce. We read these first. Please do not open a public issue for an
unpatched vulnerability.

По-русски — туда же: **info@synlabs.pro**, с описанием и шагами
воспроизведения. Пожалуйста, не открывайте публичное issue для
неисправленной уязвимости.

## What runs automatically

Every push and a nightly schedule run dependency audits (pip-audit, npm
audit), a filesystem scan (Trivy), secret scanning (gitleaks) and CodeQL
static analysis — see [.github/workflows/security.yml](.github/workflows/security.yml).
Unfixed critical CVEs in Python dependencies fail the build; documented
exceptions live next to the gate, with the reasoning inline.

## Architecture

How access control, audit and the perimeter are designed — including why
access is enforced *before* the model — is described in
[docs/en/security.md](docs/en/security.md).
