# Seekino Series Sharing and Team Scope TRD

Status: phase 1 backend and frontend implemented. The native localhost development database was backed up and migrated on 2026-10-19; its API and Web now run the workspace implementation. The separate phone/cloud database has not been migrated in this rollout. Phase 2 remains a proposal. This document covers the confirmed requirement (registered users make their own Series private or public, and public Series show who shared them) and the proposed team scope (users in several teams, teams with several members).

Background: [User/Tenant TRD](user-tenant-trd.md), [Local/Cloud design](local-cloud-trd.md) §6.

## 1. Recommendation in one paragraph

Keep `Tenant` as the container that owns content and sets authorization. Do not introduce a shared default-all tenant. Add a **Series-level read rule**: a caller can read a Series if they are a member of its tenant, or if the Series is `public`, ready, and its publisher is active. Store visibility as `series.visibility ∈ {private, public}`, where `private` means "members of the owning tenant only". For a personal tenant that means only the owner; for a future team tenant it means the whole team. This rule makes a separate `team` visibility value and a per-Series owner unnecessary for the first release. Team membership (phase 2) then replaces only the membership predicate `tenant_id == identity.tenant_id` with a membership lookup. The sharing rule does not change.

## 2. Baseline before phase 1

| Area | Current behavior | Source |
|---|---|---|
| Tenant gate | Cloud reads allow only `identity.tenant_id` or the platform public tenant. Every Series, media, search and Agent entry point calls it first | [access.py](../../../server/service/access.py) `require_tenant_read` |
| Publish | `is_published` is admin-only and only for the platform public tenant | [library.py](../../../server/api/library.py) `update_series` |
| Public readiness | `publication_ready` requires every video to be READY, indexed and playable, with no pending uploads | [media.py](../../../server/service/media.py) |
| Public listing | `list_public_series_page` loads every published Series in one tenant, then filters by readiness in Python | [library.py](../../../server/service/library.py) |
| Retrieval scope | ES filters on one `tenant_id` term plus a `series_id` and `(video_id, parse_task_id)` allow-list. Hits are re-validated against that one tenant | [elasticsearch.py](../../../server/storage/elasticsearch.py) `_scope_query` |
| Agent source | `source_tenant_id = body.source_tenant_id or body.tenant_id`. It is a single tenant, re-authorized when history evidence is read again | [agent.py](../../../server/api/agent.py) `_authorize_query_scope` |
| Media URLs | `playback_url` is a plain `/api/videos/content?…` URL that is authorized on every request (Range included). Cloud responses use `Cache-Control: private, no-store` | [media.py](../../../server/service/media.py), [api/media.py](../../../server/api/media.py) |
| Series identity | `derive_series_id(tenant_id, name)` is used on create and on file import when no explicit `series_id` is given | [identifiers.py](../../../server/model/identifiers.py), [parse_import.py](../../../server/service/parsev1/parse_import.py) |
| Default nickname | Registration sets `nickname` to the email local part | [identity.py](../../../server/repository/identity.py) `login_email` |
| Per-user records | `agent_conversation.owner_id`, `upload_session.creator_id`, `series_parse_job.creator_id`, `video_parse_task.creator_id`, `model_usage.user_id` already exist | [database.py](../../../server/database.py) `_check_identity_schema` |

Two facts make phase 1 cheap:

- **Visibility does not need to reach ES.** The search and Agent paths build the allowed Series list from MySQL before they build the ES scope. Visibility can therefore change, and be revoked, without reindexing.
- **Revocation already works on the next read.** Media URLs are not bearer tokens, and history evidence goes through `require_series_access` every time it is read. Once those checks use the new rule, revoking a public Series cuts off later reads with no further mechanism.

## 3. Phase 1 — personal public/private sharing (necessary)

### 3.1 Schema

| Change | Detail |
|---|---|
| `series.visibility` | `VARCHAR(16) NOT NULL DEFAULT 'private'`, `CHECK (visibility IN ('private','public'))`. Backfill: `'public'` where `is_published = 1`, then drop `is_published`. |
| No `series.published_at` | Not added. Discovery uses existing `create_time` and `id` ordering. |
| Index | `(visibility, create_time, id)` for the cross-tenant public listing. |
| No `series.created_by` yet | While every non-platform tenant has exactly one owner, the creator can be derived exactly from `tenant.owner_id`. It must be added before any tenant gets a second member (§4). |
| No membership table yet | Phase 1 membership is `tenant_id == identity.tenant_id` (local mode bypasses the check). |

Why rename instead of reusing `is_published`: the meaning changes from "platform-curated listing, admin-only" to "owner-controlled, readable from other tenants". Every reader of the old flag has to be reviewed anyway, and a rename makes the type checker and tests find all of them. An enum also leaves room for a later `unlisted` (link-only) value without another rename.

### 3.2 Authorization rules

Replace the tenant-first checks on Series-scoped resources with two functions in [access.py](../../../server/service/access.py):

```text
is_member(identity, tenant_id)     # phase 1: local mode, or tenant_id == identity.tenant_id
                                   # phase 2: active tenant_member row

can_read_series(identity, series):
    local mode                                   -> allow
    is_member(identity, series.tenant_id)        -> allow (full view)
    series.visibility == 'public'
      and publication_ready(series)
      and tenant.status == ACTIVE
      and (tenant.type == 'public' or owner user.status == ACTIVE)
                                                 -> allow (public view)
    otherwise                                    -> 404

can_manage_series(identity, series):
    local mode                                   -> allow
    email user and is_member(identity, series.tenant_id)  -> allow
    admin and series.tenant_id == public tenant  -> allow
    otherwise                                    -> 404
```

- **Setting visibility** requires `can_manage_series`. Switching to `public` also requires `publication_ready(series, require_published=False)`, as today.
- **Moderation takedown:** an admin can set *any* Series to `private`, so public user content can be removed. A non-member takedown accepts only the visibility field and returns `204` with no Series data; it does not grant read access to private content.
- **Tenant-scoped resources stay member-only:** upload sessions, parse jobs and tasks, parse and model usage, and conversations keep `require_tenant_read`/`require_tenant_write`. A public Series opens only its own Series, Video, image, chunk and evidence reads. It never opens other Series, uploads, jobs, usage or conversations in the same tenant.
- **Re-check order:** look up the Series by `(series_id, tenant_id)` first, check that the stored `tenant_id` matches, then apply `can_read_series`. Do not reject at the tenant gate before the lookup, which is what happens today.

Entry points to switch to `can_read_series`: `list_series`, `get_series`/`read_series`, `get_video_content` (playback and Range), `get_image_content`, `get_video_chunk`, search (`api/search.py`), Agent send (`_authorize_query_scope`), Agent history and evidence (`require_series_access`), and the SSE evidence projection.

### 3.3 What a public viewer sees

Public viewers receive title, description, cover, ready videos, durations and playback/thumbnail views. Management fields (`latest_job_id`, `last_error`, `original_filename`, `total_bytes`, `important_extra_data`, transcript hints) are null; `pending_uploads` and `character_bible` are empty. Original filenames are also hidden in chunk/search/Agent history/SSE evidence responses. `variant=original` is member-only; public viewers use `playback`.

This is a response projection, not a separate confidential model context: parsing and Agent inference still use the Series background and character information, and known Series image URLs are readable when their Series is public. Those inputs may affect generated answers. Keeping selected background information confidential from public inference would require a separate model/tool input policy and is outside this backend batch.

### 3.4 Publisher (sharer) identity

Return `publisher` on every Series summary and detail, derived from the owning tenant:

| Tenant type | `publisher` |
|---|---|
| `personal` / `local` | `{kind: "user", display_name: owner.nickname, avatar: ImageAsset \| null}` |
| `public` (platform) | `{kind: "platform", display_name: tenant.name, avatar: tenant avatar \| null}` |
| `team` (phase 2) | `{kind: "team", display_name: tenant.name, avatar: …}`. A per-uploader byline is optional, from `created_by` |

Contracts that must be fixed first:

- **Privacy:** new accounts use a generated `用户-<short hash>` nickname. Public projection substitutes the same generated name for empty nicknames or legacy nicknames matching the owner's email/local part. Custom nicknames are preserved. No email is returned in `publisher`; no new User field is needed.
- **Avatar format:** phase 1 returns `null` and the UI uses a placeholder. A future avatar upload must store an image ID in the existing image store and return an `ImageAsset`, rather than expose the existing free-form avatar field.

### 3.5 API changes

| Endpoint | Change |
|---|---|
| `POST /api/series/update` | `visibility` replaces `isPublished`. Owner-managed, plus an admin `private` takedown. Turning public is rejected unless the Series is ready. Member updates return Series detail; non-member admin takedowns return `204`. |
| New `GET /api/series/public` | Cross-tenant discovery listing of public, ready Series with `publisher`, paginated. The cursor scope must not include the caller's tenant. |
| `GET` series list / detail | Add `publisher` and `access: "member" \| "public"` so the UI knows when to hide edit, upload and job controls. `tenantId` stays as an addressing parameter that is checked against the stored value. |
| Search / Agent send | `sourceTenantId` may name another user's tenant, but only with explicit `seriesIds`, each checked with `can_read_series`. An empty `seriesIds` means "all public Series" **only** for the platform public tenant, as today, which prevents enumerating another tenant. |
| Media / image / chunk | Same URLs. Authorization goes through `can_read_series`, and `original` is member-only. |

Agent turns reauthorize both the selected source and all historical sources reused as model input, including Series referenced through playback context. Checks run before model work, before JSON output, and before/after waiting for each SSE event. Revoking a historical source can therefore make a conversation unreadable/unusable until that source is accessible again; the user may start a new conversation with other sources.

Cross-publisher search in one query is **not** in phase 1 (see §5).

### 3.6 Migration (phase 1)

- Migration: `docs/migrations/20261019_series_visibility.sql` adds `visibility`, backfills from `is_published`, adds its constraint/index and drops the old column. ORM, startup schema checks and backend DTO/VO are updated. Frontend contracts/consumers must be updated before switching a running deployment. The application does not run this migration automatically.
- No ES migration and no reindex. No change to tenant IDs or Series ID derivation.
- Local mode: no behavior change (local bypasses checks, and `visibility` is stored but has no effect). The local and phone cloud instances keep separate processes and data namespaces. Publishing from a local instance to the cloud is out of scope.

### 3.7 First release: whole-Series readiness

`publication_ready` is all-or-nothing. With user uploads, adding episode 5 to a public Series hides episodes 1–4 until it is processed. Options:

1. Keep all-or-nothing (current behavior, simplest; document the UX).
2. Make the Series visible when at least one video is ready, and give public viewers and retrieval scopes only the ready videos. ES already scopes by `(video_id, parse_task_id)` pairs, so this is feasible without index changes.

Phase 1 keeps option 1. Option 2 remains a future enhancement; it is not required for correctness.

### 3.8 Acceptance matrix

Run each endpoint in §3.2 for each viewer (guest, other user, owner, admin) against each Series state (private; public + ready; public + not ready; public then revoked; publisher disabled). Also cover:

- Range requests after revocation.
- Agent history evidence re-read after revocation.
- SSE evidence projection.
- Tenant-scoped endpoints (uploads, jobs, usage, conversations) stay 404 for non-members of a tenant that has a public Series.
- Two users of the same public Series keep separate conversations.

### 3.9 Frontend implementation

- Series cards, details and the selected library Series expose a vertical ellipsis visibility menu only when the server returns `access: "member"`. Updates address the Series's own tenant and invalidate list/detail caches after success. Other users have no editing menu; server authorization remains authoritative.
- Public data discovery uses `/api/series/public`. Public cards/details show the publisher with an initials fallback until managed avatar uploads exist.
- Series and playback links carry the resource's `tenantId`; detail cache keys include it. The active account and connection are unchanged when viewing another publisher.
- Chat keeps conversation/attachment ownership in the viewer's active scope and sends a separate `sourceTenantId` for retrieval. Each query selects Series from one publisher. Playback chat stays within its video's source, and historical clip follow-up selects the referenced Series rather than the default publisher.
- This release adds no moderation dashboard. Platform administrators retain the backend's restricted takedown operation; the owner menu does not grant them arbitrary private-content editing.
- Local visibility controls persist the flag for testing. They do not upload or synchronize local files into a separate cloud instance.

## 4. Phase 2 — team scope (optional until confirmed)

### 4.1 Model

| Change | Detail |
|---|---|
| `tenant.type` | Add `'team'`. A team tenant requires `owner_id` (creator/billing owner) and gets its own `name` and `avatar`. |
| `tenant_member` | `id, tenant_id, user_id, role, status, invited_by, create_time, update_time`; `UNIQUE(tenant_id, user_id)`; `role ∈ {owner, admin, member}`; `status ∈ {invited, active, removed}`. |
| Backfill | One `owner/active` row per existing `local` and `personal` tenant, so membership is the only read path. `owner_id` stays for constraints and accounting. |
| `series.created_by` | `VARCHAR(ID_LENGTH) NULL` FK to `user`. Backfill exactly from `tenant.owner_id` for personal and local tenants, `NULL` for the platform tenant. It must exist before any tenant gets a second member. |

Unlike RAGFlow, a personal tenant is never joined by others. Teams are separate tenants. This avoids RAGFlow's dependence on `tenant.id == user.id` and its `me` visibility. "Private" stays tenant-scoped, and the creator is a separate column.

### 4.2 Authorization

- `is_member` becomes an active-membership lookup that runs **on every request** (or is cached with explicit invalidation). Do not put it in the token, so removal takes effect on the next request.
- `AuthIdentity.tenant_id` becomes the *current / home* tenant used for defaults. It is not the authorization set.
- Proposed role matrix (to confirm):

| Action | member | admin | owner |
|---|---|---|---|
| Read all team Series | ✓ | ✓ | ✓ |
| Create Series, upload, start parse | ✓ | ✓ | ✓ |
| Edit metadata / episode order | own (`created_by`) | all | all |
| Delete Series | own | all | all |
| Set visibility `public` | — | ✓ | ✓ |
| Invite / remove members, change roles | — | ✓ (not owners) | ✓ |
| Delete team / transfer ownership | — | — | ✓ |

- Personal records stay personal inside a team: conversations stay filtered by `owner_id`. Whether admins can see team usage (`model_usage`, parse usage) is a separate decision. The safe default is per-user only.
- Global admin (`is_superuser`), tenant role, resource creator and resource visibility stay four separate checks, and none implies another.

### 4.3 API

- Team CRUD, invitations (email based, accept/decline, expiry), leave and remove (the last owner cannot leave), role changes.
- `GET /api/me/tenants` and a tenant switcher. The frontend cancels old requests and SSE when switching, as required by the user/tenant TRD §7.

### 4.4 Sharing a personal Series with a team

Phase 2 recommends **create in team** only. Moving an existing Series between tenants has to rewrite `tenant_id` in MySQL, in ES documents and in tenant-carrying child tables. The moved Series also no longer matches `derive_series_id(new_tenant, name)`, so a later file import by name without an explicit `series_id` would create a duplicate. A move feature must therefore always import by explicit ID. A grant table (share one Series with another tenant) is a further option and is not needed.

### 4.5 Migration (phase 2)

SQL: widen the `ck_tenant_type` CHECK, create `tenant_member`, backfill owner rows, add and backfill `series.created_by`. No ES change: retrieval scope stays per tenant and per Series.

## 5. Cross-publisher search (separate decision)

ES `_scope_query`, `_TrustedScope` and Agent `sourceTenantId` each assume one tenant. To search "all public Series" across publishers in one query, change the trusted scope to a set of `(tenant_id, series_id)` pairs: use `terms` on `tenant_id`, keep the per-video `(video_id, parse_task_id)` allow-list, and validate each hit against its own pair. Persist `sourceTenantIds` (or per-Series tenant) in Agent message detail instead of a single `sourceTenantId`. Until then, the platform public tenant stays the default "search everything" pool, and user-published Series are searched one publisher at a time.

## 6. Rejected options

| Option | Reason |
|---|---|
| Shared default-all tenant | Removes the basis for content permissions and changes every derived Series ID. It would need a new ownership column on every tenant-carrying table. |
| Toggle `is_published` for users without changing the read path | Other tenants would still be rejected by `require_tenant_read`, and the public listing only queries the platform tenant. |
| Moving user Series into the platform public tenant to "publish" | Loses ownership, changes the derived ID, and lets the platform admin edit user content. |
| Copying RAGFlow `me`/`team` | Gives no anonymous public access, and `me` depends on `tenant.id == user.id`. |
| Per-Series `team` visibility in phase 1 | There are no team tenants yet. In this model, tenant-private already means team-visible. |

## 7. Order of work

1. Phase 1 schema and migration, `can_read_series` / `can_manage_series`, switching every entry point in §3.2, public projection, admin takedown.
2. Publisher contract (nickname confirmation, avatar image ID) and `GET /api/series/public`.
3. Frontend: visibility control, publisher byline, `access`-based UI, discovery list.
4. Readiness granularity (§3.7), if chosen.
5. Phase 2 team model only after the role matrix is confirmed. Cross-publisher search (§5) is independent of phase 2.
