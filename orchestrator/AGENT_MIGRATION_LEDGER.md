# Agent Migration Ledger

Last updated: 2026-03-11

This ledger is the consolidated migration source of truth for the backend agent ecosystem.

## Canonical Targets

| Target | Canonical ID | Current State | Runtime Status | Notes |
|---|---|---|---|---|
| Ava User | `ava_user` | migrated to new template contract | active | Frontend user-facing Ava orchestration surface; Wave 6 complete |
| Ava Admin | `ava_admin` | migrated to new template contract | active | Back-office admin portal Ava surface; Wave 6 complete |
| Sarah | `sarah_front_desk` | migrated to new template contract | active | Specialist telephony/front desk; Wave 2 complete |
| Eli | `eli_inbox` | migrated to new template contract | active | Inbox/mail specialist; Wave 2 complete |
| Quinn | `quinn_invoicing` | migrated to new template contract | active | Invoicing specialist; Wave 2 complete |
| Nora | `nora_conference` | migrated to new template contract | active | Conference specialist; Wave 1 complete |
| Adam | `adam_research` | migrated to new template contract | active | Research specialist; Wave 1 complete |
| Tec | `tec_documents` | migrated to new template contract | active | Document specialist; Wave 1 complete |
| Finn | `finn_finance_manager` | migrated to new template contract | active | Finance specialist; Wave 5 complete |
| Milo | `milo_payroll` | migrated to new template contract | active | Payroll specialist; Wave 5 complete |
| Teressa | `teressa_books` | migrated to new template contract | active | Books specialist; Wave 4 complete |
| Clara | `clara_legal` | migrated to new template contract | active | Legal specialist; Wave 5 complete |
| Mail Ops | `mail_ops_desk` | migrated to new template contract | active | Internal admin mail operations; Wave 4 complete |
| SRE Triage | `sre_triage` | activated | active | Internal ops pack activated on template |
| QA Evals | `qa_evals` | activated | active | Internal ops pack activated on template |
| Security Review | `security_review` | migrated to new template contract | active | Internal ops pack activated on template; Wave 4 complete |
| Release Manager | `release_manager` | activated | active | Internal ops pack activated on template |

## Migration Waves

| Wave | Targets | Goal |
|---|---|---|
| 0 | Inventory reconciliation | Freeze canonical ids and runtime mappings |
| 1 | `adam_research`, `tec_documents`, `nora_conference` | Low-risk proof migration |
| 2 | `sarah_front_desk`, `eli_inbox`, `quinn_invoicing` | Communication-heavy migration |
| 3 | `sre_triage`, `qa_evals`, `release_manager` | Internal ops activation and policy validation |
| 4 | `teressa_books`, `mail_ops_desk`, `security_review` | Internal/admin and finance-adjacent cutover |
| 5 | `finn_finance_manager`, `milo_payroll`, `clara_legal` | Highest-risk business workflows |
| 6 | `ava_user`, `ava_admin`, reserved slot | Final orchestration layer migration complete |

## Required Artifact Contract

Every target above must own these runtime artifacts:

- skill pack module
- pack manifest
- persona
- risk policy
- tool policy
- autonomy policy
- observability policy
- prompt contract
- validation and certification coverage

## Internal-Only Contract

These targets are internal-only and must never be user-routable without an approved admin bridge:

- `ava_admin`
- `mail_ops_desk`
- `sre_triage`
- `qa_evals`
- `security_review`
- `release_manager`
