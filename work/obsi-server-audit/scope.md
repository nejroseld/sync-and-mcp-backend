# Case Scope

## meta
- case_id: obsi-server-audit
- created: 2026-10-07T10:53:09+02:00
- operator: local
- project_root: /workspace/obsi-mcp/server
- primary_skill: reverse-engineering/SKILL.md
- primary_id: R0
- lead_role: lead
- specialist_roles: []
- hint: server-side security audit of local FastAPI source code
- preset: offline-sample

## auth
- status: granted
- basis: own_system
- evidence_of_auth: owner-operated local repository /workspace/obsi-mcp/server
- MUST NOT proceed if status != granted

## in_scope
- assets:
  - /workspace/obsi-mcp/server/obsi_server/app.py
  - /workspace/obsi-mcp/server/obsi_server
  - /workspace/obsi-mcp/server/scripts
- surfaces: []
- activities: []

## out_of_scope
- assets: []
- activities: [dos, phishing_real_users, unrestricted_exfil]

## network_profile
- mode: offline
- notes: |
    offline | lab_only | authorized_target_only | unrestricted_lab
    Change mode only after auth.status = granted.
    Presets: offline-sample | ctf-public | own-system

## deliverables
- report: true
- field_journal: true
- diagrams: true
- timeline: true

## constraints
- timebox: {}
- stealth: low
- data_handling: anonymize

## signoff
- ready_for_act: true
- checklist:
  - [x] auth.status = granted
  - [x] in_scope.assets non-empty OR offline sample path set
  - [x] network_profile.mode chosen
  - [ ] out_of_scope reviewed
  - [ ] roles assigned (see skills/ops/role-map.md)

## ops_refs
- skills/ops/scope-contract.md
- skills/ops/evidence-finding-path.md
- skills/ops/role-map.md
- skills/ops/timeline-workitem.md
- skills/ops/IDENTITY.md
