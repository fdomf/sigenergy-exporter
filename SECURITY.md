# Security Policy

## Supported Versions

Security fixes are provided for the latest released minor version.

| Version | Supported |
| ------- | --------- |
| 0.2.x   | :white_check_mark: |
| 0.1.x   | :x: |

This table will be updated when a new minor version is released.

## Reporting a Vulnerability

Please do not report suspected security vulnerabilities through public GitHub
issues or discussions.

Use GitHub's private vulnerability reporting feature:

1. Open the repository's **Security and quality** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Please include, when possible:

- The affected exporter version or commit.
- A description of the vulnerability and its potential impact.
- Steps or a minimal example that reproduces the issue.
- Any suggested mitigation or fix.
- Relevant logs with credentials, addresses, and personal information removed.

Please only test against systems and equipment that you own or are authorized
to access.

This is a volunteer-maintained project. A reasonable effort will be made to
acknowledge reports within seven days and provide updates as the investigation
progresses, but response times cannot be guaranteed.

Please allow time for a fix to be developed and released before publicly
disclosing a vulnerability.

## Scope

Issues in the exporter source code, container image, dependencies, build
workflows, and handling of untrusted input are within scope.

Vulnerabilities in Sigenergy hardware, firmware, cloud services, or the Modbus
protocol itself should be reported directly to Sigenergy unless the issue is
caused or exposed specifically by this exporter.
