# Checklist (Claude)

## Before wiring backend↔admin↔app
- [ ] Bootstrap workspace (or update robots.config.yaml paths)
- [ ] Run validate mode and ensure **all 4 roots pass**

## Before any merge to main
- [ ] Run validate mode
- [ ] If release-related: run smoke mode with `repo_health` + `api_smoke`

## Before a production promotion
- [ ] Run smoke mode with `repo_health` + `api_smoke` + `ui_smoke`
- [ ] Ensure evidence artifacts exist
- [ ] Post RobotRun to ingest (or attach artifacts to release)
