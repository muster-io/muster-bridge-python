# Security policy

## Reporting a vulnerability

Report a vulnerability privately, through GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/muster-io/muster-bridge-python/security/advisories) of the repository and choose
**Report a vulnerability**, or go straight to <https://github.com/muster-io/muster-bridge-python/security/advisories/new>.
Do not open a public issue, pull request or discussion about it.

A vulnerability in the Bridge protocol itself, rather than in this library, belongs to Muster: report it as Muster's
[security policy](https://github.com/muster-io/muster/blob/master/SECURITY.md) says.

Include in the report:

- a description of the vulnerability and its impact;
- steps to reproduce it, or a proof of concept;
- the affected versions;
- a suggested fix, if you have one.

## Response process

We acknowledge a report within 7 days and agree with you on a disclosure date. The fix ships as a new patch release of
the latest minor release, together with a GitHub security advisory, which can carry a CVE that GitHub assigns. Tell us
if you would like to be credited in the advisory.

## Supported versions

Only the latest minor release is supported: a security fix ships as a new patch release of it, and older minor
releases are not patched. Before 1.0 the same holds for the latest 0.y release.

## Supply-chain checks

- On every pull request, every push to `master` and daily, CI runs the licence check, which fails on a runtime
  dependency outside the permissive licences the project allows.
- CodeQL analyses the Python code and the workflows on every pull request, on every push to `master` and weekly.
- OpenSSF Scorecard evaluates the repository's practices on every push to `master` and weekly.
- CodeQL and Scorecard findings appear in the repository's code scanning alerts.
- Renovate opens pull requests that update dependencies, and opens security updates without waiting for its weekly
  schedule; the development tools are locked in `uv.lock`, uv is pinned in the workflows, and GitHub Actions are pinned
  by commit SHA.
- Releases are published to PyPI by trusted publishing, without a stored token, with attestations that tie each file
  to the workflow run that built it.
