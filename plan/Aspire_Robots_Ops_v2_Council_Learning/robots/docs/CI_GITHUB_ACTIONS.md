# GitHub Actions template

Robots are packaged as a separate zip, but you can also run them in CI.

See `robots/workflows/github/robots.yml` for a copy/paste workflow.

Typical pattern:
1) checkout repo that contains this robots folder
2) bootstrap workspace or point config to checked out repos
3) run validate (PR) and smoke (main/release)
4) upload `robots/out/evidence/*` as artifacts
5) optionally POST `RobotRun` to backend ingest
