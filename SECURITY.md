# Security policy

## Report a vulnerability privately

Please do not open a public issue for a suspected vulnerability. Use GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Advisories** and then **Report a vulnerability**.
3. Describe the affected version or commit, impact, reproduction steps, and any suggested mitigation.

This creates a private draft Security Advisory that repository maintainers can discuss with you. If private
reporting is unavailable, do not publish the details; use GitHub to contact the repository owner and ask for a
private reporting channel.

Do not include secrets, petition data, or intimate or private media in a report. Use the smallest synthetic or
non-sensitive proof of concept that demonstrates the problem.

## Scope

Security reports are especially useful for issues involving:

- upload streaming, file-type detection, temporary-file cleanup, hostile media parsing, decoder isolation, or
  parser resource limits;
- C2PA manifest validation, signer or timestamp trust, trust-list handling, or a path that could produce a
  verified verdict without the required cryptographic trust;
- petition export and erasure admin endpoints, bearer-token handling, authentication rate limits, or disclosure
  of petition data;
- the local MCP server, including allowed-directory escapes, symlink handling, unintended file access, command
  execution, or a network-access path; and
- dependency or deployment behavior that could expose uploaded media, credentials, personal data, or the host.

Ordinary wrong or inconclusive scanner verdicts are usually quality reports rather than vulnerabilities. Use
the false-result issue form unless the result comes from a trust-boundary bypass, unsafe parser behavior, or
another security flaw.

## Safe research

Test only a deployment and data that you own or have explicit permission to test. Do not probe other people's
BanTheFake deployments, access petition records, evade rate limits on public services, degrade availability, or
use deceptive or intimate media of real people. Stop if testing could expose personal data or affect other
users.

## What to expect

BanTheFake is a student-run project, so it cannot promise a formal service-level agreement or bounty. A
maintainer will try to acknowledge a report within seven days, assess it, and provide updates when there is
material progress. Complex issues may take longer. Please allow time for a fix and coordinated disclosure
before publishing details.
