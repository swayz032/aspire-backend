# Vulnerability Management Checklist

## CI/CD Security Scanning

- [ ] **Dependency scanning**: Automated in CI pipeline
  - Python: `pip-audit` or `safety` for `pyproject.toml` dependencies
  - TypeScript: `npm audit` or `pnpm audit` for `package.json` dependencies
  - Run on every PR and weekly scheduled scan

- [ ] **Secret scanning**: Automated in CI pipeline
  - GitHub secret scanning enabled on repository
  - Pre-commit hooks for detecting secrets in staged files
  - Scan scope: source code, config files, documentation

- [ ] **Static analysis**: Code quality + security linting
  - Python: `ruff` + `bandit` for security-specific checks
  - TypeScript: `eslint` with security plugin

## Patch Cadence

- **Weekly**: Review dependency update notifications (Dependabot / Renovate)
- **Critical CVEs**: Patch within 24 hours of disclosure
- **High CVEs**: Patch within 7 days
- **Medium/Low CVEs**: Patch within 30 days or next release cycle

## Incident Response

- Incident response runbook: `docs/operations/incident_response.md`
- Kill switch procedure: `docs/operations/kill_switch.md`
- Escalation path: On-call engineer -> Engineering lead -> CTO

## Security Contact

- Internal: Engineering team via incident channel
- External: security@aspireos.app (to be configured)
- Responsible disclosure policy: To be published at `www.aspireos.app/.well-known/security.txt`

## Review Schedule

- **Monthly**: Review open vulnerability reports, verify patch compliance
- **Quarterly**: Full security review against Gate 5 checklist (5 pillars)
- **Annually**: Third-party penetration test (required for partner approvals)
