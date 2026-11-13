# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.**

Use one of these private channels:

1. **GitHub private vulnerability reporting.** Open the [Security tab](https://github.com/UiPath/skills-library/security/advisories/new) of this repository and click "Report a vulnerability".
2. **UiPath Bug Bounty / HackerOne.** Report through [HackerOne](https://hackerone.com/uipath) or email **hackerone@uipath.com**.
3. **UiPath Trust.** For other security or trust queries, use [trust.uipath.com](https://trust.uipath.com/) or email **trust@uipath.com**.

Include a description of the issue and its impact, steps to reproduce, the skill involved, and any suggested fix. We acknowledge reports within 5 business days.

## Scope Notes

Skills are instructions that AI coding agents follow on a developer's machine, with that developer's local privileges and UiPath credentials. Skills in this repo are written by the community and approved by a UiPath steward; UiPath has not run them end to end unless they are marked Verified. In scope:

- **Skill content** that could steer an agent into unsafe actions: exfiltrating credentials, running destructive commands, or acting on injected instructions.
- **Leaked data** in a skill: secrets, tenant identifiers, customer names, real sample documents.
- **The intake workflows** under `.github/`, including any way for a pull request to run code with repository permissions.

A skill found to be unsafe is moved to `archived/` and removed from the install path while it is investigated.
