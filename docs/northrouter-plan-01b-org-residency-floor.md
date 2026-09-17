# NorthRouter plan 01b: organization residency floor

Status: design, not yet implemented. Follows plan 01 (`feat/sovereignty-residency-routing`,
PR #1), which added the 4-level sovereignty scale, per-request residency bars,
provider attestations, and `residency_audit`.

## Problem

Plan 01 makes residency a property of the *request*: a caller opts in by sending
`residency: "sovereign"`. An organization cannot compel its own traffic. The
existing tenant controls (`allowed_models` on keys and users, workspace provider
overrides) are per-credential ceilings: a member with key-creation rights can
mint a key with `allowed_models: null` and reach every model. There is no way to
say "all of this organization's requests may only be served by sovereign models."

## Deployment context (decided 2026-09-17)

- This deployment hosts only admin-added, admin-vetted providers. Customers who
  want local models route inside their own network and escalate to NorthRouter
  only for models we have added. No BYO providers.
- Provider attestations are admin-only. Organizations cannot write attestation
  rows, cannot downgrade, and cannot inflate a level the platform did not verify.
- The request-side wire vocabulary from plan 01 (`canadian` / `sovereign` /
  `sovereign_model`, the 1-4 internal scale, `residency_audit`) is a stable
  contract for plans 02 (billing) and 03 (portal). 01b changes none of it.

## Design

### 1. Storage

One value per organization: the residency floor.

- `null` = no floor (default; today's behavior).
- A bar name (`canadian`, `sovereign`, `sovereign_model`) = every request from
  this organization must clear that level.

Preferred home: the organization guardrails surface (the table established by
`d8b3f1c6a4e9` already carries "org adds restrictions, no workspace veto"
semantics). A column on `organization` is the fallback if the guardrails shape
fits poorly. One new alembic revision either way, chained after
`d6a8c0e2f4b6`.

### 2. Combination rule

```
required_level = max(request_bar_level, org_floor_level)
```

- No `residency` field + org floor `sovereign` -> level 3 enforced.
- Request `canadian` under a `sovereign` floor -> level 3 anyway.
- A request bar can only raise the requirement, never lower the org's.
- The floor cannot be lifted from the request body, the key, the workspace, or
  any client-supplied field. It is resolved from the organization the billed key
  belongs to, so no key configuration escapes it: `allowed_models: null` means
  "no per-key restriction", not "exempt from org policy".

### 3. Enforcement

`_pipeline.py` already resolves the request's organization (`residency_org_id`)
from the billed key's workspace before loading sovereignty rows. The floor is
read in the same step and folded into `required_sovereignty` before the gates
run. No gate changes:

- compiler candidate filter (failover included) consumes the higher bar as-is;
- head-candidate gate for plain and alias selectors consumes it as-is;
- `services/sovereignty_access.py` stays pure and synchronous: the floor is an
  input to the compile, not a database read, so CLI `explain` keeps working.

### 4. Catalog consistency

`GET /api/v1/models` applies the caller's org floor automatically: a member of
a sovereign-floor organization sees only sovereign models even without passing
`residency=`. The catalog never advertises a model the caller's own org policy
would refuse at inference, and the floor is discoverable by looking at the
catalog. An explicit `residency=` query param still composes with the floor
under the same `max()` rule.

### 5. Audit

`residency_audit` gains one field: the effective bar's source
(`"request"`, `"organization"`, or both when they tie). Existing fields
(`requested_residency`, `served_sovereignty_level`) are unchanged, so plans
02/03 read them as contracted; the source field is additive.

### 6. Refusal copy

A 403 caused by the org floor names the floor explicitly ("this organization
requires sovereign hosting") rather than resembling a failed caller-supplied
bar. Debugging a policy you did not write is much easier when the refusal says
who set it.

## Explicitly out of scope

- Org-writable attestations (admin-only; the portal in plan 03 must not expose
  attestation writes to orgs).
- Org downgrade or inflation of deployment attestations (no `min()` clamp needed
  because orgs write no attestation rows at all).
- BYO providers (policy-excluded; the level-1 fail-closed default already
  covers an unattested provider if one ever slips in).
- Per-workspace floors. The floor is org-wide; workspaces inherit. Revisit if a
  customer needs workspace-level variation.

## Verification plan

- Unit: `max()` combination table (floor x request bar x absent), floor
  resolution from billed key's org, refusal detail naming the source.
- Integration: a member key with `allowed_models: null` under a sovereign floor
  is refused a non-sovereign model (the anti-escape case); catalog listing
  honors the floor without the query param; audit rows carry the source.
- Migration round-trip on the new revision.

## Contract impact

None on the plan 01 wire vocabulary. Plans 02/03 contract against the enum, the
`residency` param, and `residency_audit` unchanged; 01b only changes how
`required_sovereignty` is computed server-side and adds one audit field.
