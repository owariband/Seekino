"""Series-shared editing revisions. Each mutation locks its series-scoped template."""

from contextlib import contextmanager
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import defer

from ..model.dto.editing_template import TemplateDetails, canonical_bytes, validate_files, validate_note
from ..model.editing_template import EditTemplate, EditTemplateVersion
from ..utils.time import now_unix_time_ms


def _hash(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _version_id(tenant, series_id, author, request_id):
    return str(uuid5(NAMESPACE_URL, canonical_bytes(["seekino-edit", tenant, series_id, author, request_id]).decode()))


def _receipt(version):
    return {"template_id": version.template_id, "version_id": version.id, "version_no": version.version_no}


def _attest(version):
    return _hash([version.template_id, version.id, version.version_no, version.parent_version_id,
                  version.author, version.create_time, version.body])


def _summary(template):
    return {"template_id": template.id, "tenant_id": template.tenant_id, "series_id": template.series_id,
            "creator_id": template.creator_id, "name": template.name, "description": template.description,
            "tags": template.tags, "revision": template.revision, "default_version_id": template.default_version_id}


def _note(entry):
    return {key: value for key, value in entry.items() if key not in {"request_id", "request_hash"}}


def _version_summary(version, template):
    return {**_receipt(version), "parent_version_id": version.parent_version_id, "author": version.author,
            "created_at": version.create_time, "is_default": version.id == template.default_version_id,
            "latest_evaluation": _note(version.evaluation[-1]) if version.evaluation else None,
            "latest_check": _note(version.checks[-1]) if version.checks else None}


def _template(session, tenant, series_id, template_id, *, lock=False):
    query = select(EditTemplate).where(EditTemplate.tenant_id == tenant, EditTemplate.series_id == series_id,
                                       EditTemplate.id == template_id)
    record = session.scalar(query.with_for_update() if lock else query)
    if record is None:
        raise ValueError("TEMPLATE_NOT_FOUND")
    return record


def _versions(session, template):
    return list(session.scalars(select(EditTemplateVersion).options(defer(EditTemplateVersion.body)).where(
        EditTemplateVersion.template_id == template.id).order_by(EditTemplateVersion.version_no)))


def _version(session, template, version_id):
    version = session.scalar(select(EditTemplateVersion).options(defer(EditTemplateVersion.body)).where(
        EditTemplateVersion.template_id == template.id, EditTemplateVersion.id == version_id))
    if version is None:
        raise ValueError("TEMPLATE_NOT_FOUND")
    return version


def _same_request(stored, requested):
    if stored != requested:
        raise ValueError("TEMPLATE_CONFLICT: The request_id was used with different input.")


class EditingTemplateRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    @contextmanager
    def _write(self):
        with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session

    def search(self, scopes, query, limit=5, tags=None):
        if not scopes:
            return []
        with self.sessions() as session:
            # Name and description are free text. Tag membership is exact, including non-ASCII on SQLite.
            if session.get_bind().dialect.name == "sqlite":
                tag_values = func.json_each(EditTemplate.tags).table_valued("value")
                def has_tag(tag):
                    return select(1).select_from(tag_values).where(tag_values.c.value == tag).exists()
            else:
                def has_tag(tag):
                    return func.json_contains(EditTemplate.tags, canonical_bytes(tag).decode()) == 1
            shared = or_(*(and_(EditTemplate.tenant_id == tenant, EditTemplate.series_id == series_id)
                           for tenant, series_id in scopes))
            rows = session.scalars(select(EditTemplate).where(
                shared,
                or_(EditTemplate.name.icontains(query, autoescape=True), EditTemplate.description.icontains(query, autoescape=True),
                    has_tag(query)), *(has_tag(tag) for tag in tags or ()),
            ).order_by(EditTemplate.update_time.desc(), EditTemplate.id).limit(limit))
            result = []
            for row in rows:
                default = _version(session, row, row.default_version_id) if row.default_version_id else None
                result.append({**_summary(row), "default_version": _version_summary(default, row) if default else None})
            return result

    def scope(self, *, template_id=None, version_id=None):
        with self.sessions() as session:
            query = select(EditTemplate)
            if template_id:
                query = query.where(EditTemplate.id == template_id)
            if version_id:
                query = query.join(EditTemplateVersion).where(EditTemplateVersion.id == version_id)
            template = session.scalar(query) if template_id or version_id else None
            if template is None or template.series_id is None:
                raise ValueError("TEMPLATE_NOT_FOUND")
            return template.tenant_id, template.series_id

    def versions(self, tenant, series_id, template_id):
        with self.sessions() as session:
            template = _template(session, tenant, series_id, template_id)
            return {**_summary(template), "versions": [_version_summary(v, template) for v in _versions(session, template)]}

    def read(self, tenant, series_id, template_id, version_id=None):
        with self.sessions() as session:
            template = _template(session, tenant, series_id, template_id)
            versions = _versions(session, template)
            selected = version_id or template.default_version_id
            if not selected:
                return {**_summary(template), "versions": [_version_summary(v, template) for v in versions]}
            version = _version(session, template, selected)
            if _attest(version) != version.content_hash:
                raise ValueError("TEMPLATE_CONFLICT: Saved content failed its integrity check.")
            descendants = {version.id}
            successor_notes = []
            for other in versions:
                if other.parent_version_id in descendants:
                    descendants.add(other.id)
                if other.id != version.id:
                    successor_notes.extend({"version_id": other.id, **_note(note)} for note in other.evaluation
                                           if note.get("about_version_id") == version.id or
                                           (other.id in descendants and note["kind"] == "revision_rationale"))
            return {**_summary(template), "version": {**_version_summary(version, template), **version.body,
                    "content_hash": version.content_hash, "evaluation": [_note(n) for n in version.evaluation],
                    "check": [_note(n) for n in version.checks], "successor_notes": successor_notes}}

    def _saved(self, session, tenant, series_id, version_id, digest):
        row = session.scalar(select(EditTemplateVersion).join(EditTemplate).where(
            EditTemplate.tenant_id == tenant, EditTemplate.series_id == series_id, EditTemplateVersion.id == version_id))
        if row is not None:
            _same_request(row.request_hash, digest)
            return _receipt(row)

    def save(self, tenant, series_id, *, author, template_id, new_template, parent_version_id, content, evaluation,
             check, request_id, source_refs, resolve_source):
        if bool(template_id) == (new_template is not None) or (new_template is not None and parent_version_id):
            raise ValueError("INVALID_ARGUMENT: Choose one new or existing template.")
        metadata = TemplateDetails.model_validate(new_template).model_dump() if new_template is not None else None
        evaluation = validate_note(evaluation, "evaluation")
        check = validate_note(check, "check") if check is not None else None
        if parent_version_id and (evaluation["kind"] != "revision_rationale" or not (evaluation.get("changes") or evaluation.get("context"))):
            raise ValueError("INVALID_ARGUMENT: Explain the reason for this revision.")
        validate_files(content["files"], content["entry"])
        if len(canonical_bytes({k: v for k, v in content.items() if k != "files"})) > 65536:
            raise ValueError("INVALID_ARGUMENT: Template contract exceeds 64 KiB.")
        version_id = _version_id(tenant, series_id, author, request_id)
        digest = _hash([template_id, metadata, parent_version_id, content, evaluation, check, source_refs])
        with self.sessions() as session:
            existing = self._saved(session, tenant, series_id, version_id, digest)
            if existing:
                return existing
            if template_id:
                _template(session, tenant, series_id, template_id)
        body = {**content, "sources": [resolve_source(ref) for ref in dict.fromkeys(source_refs)]}
        # A simultaneous retry can insert the deterministic id first. Re-read its receipt after rollback.
        try:
            with self._write() as session:
                if metadata is not None:
                    template = EditTemplate(id=str(uuid5(NAMESPACE_URL, "template:" + version_id)),
                                            tenant_id=tenant, series_id=series_id, creator_id=author, **metadata)
                    session.add(template)
                    session.flush()
                else:
                    template = _template(session, tenant, series_id, template_id, lock=True)
                existing = self._saved(session, tenant, series_id, version_id, digest)
                if existing:
                    return existing
                for linked in {parent_version_id, evaluation.get("about_version_id")} - {None}:
                    _version(session, template, linked)
                latest = session.scalar(select(func.max(EditTemplateVersion.version_no)).where(
                    EditTemplateVersion.template_id == template.id)) or 0
                stamp = now_unix_time_ms()
                attribution = {"author": author, "created_at": stamp}
                version = EditTemplateVersion(id=version_id, template_id=template.id, version_no=latest + 1,
                    parent_version_id=parent_version_id, author=author, create_time=stamp, body=body, request_hash=digest,
                    evaluation=[{**evaluation, **attribution}], checks=[{**check, **attribution, "reported_by": "client"}] if check else [])
                version.content_hash = _attest(version)
                session.add(version)
                template.update_time = stamp
                return _receipt(version)
        except (IntegrityError, OperationalError):
            with self.sessions() as session:
                existing = self._saved(session, tenant, series_id, version_id, digest)
                if existing:
                    return existing
            raise

    def append(self, tenant, series_id, *, author, version_id, kind, entry, request_id):
        note = validate_note(entry, kind)
        digest = _hash([kind, note])
        with self._write() as session:
            template = session.scalar(select(EditTemplate).join(EditTemplateVersion).where(
                EditTemplate.tenant_id == tenant, EditTemplate.series_id == series_id,
                EditTemplateVersion.id == version_id).with_for_update())
            if template is None:
                raise ValueError("TEMPLATE_NOT_FOUND")
            version = _version(session, template, version_id)
            if note.get("about_version_id"):
                _version(session, template, note["about_version_id"])
            for prior in [*version.evaluation, *version.checks]:
                if prior.get("request_id") == request_id and prior.get("author") == author:
                    _same_request(prior["request_hash"], digest)
                    return _receipt(version)
            recorded = {**note, "author": author, "created_at": now_unix_time_ms(), "request_id": request_id, "request_hash": digest}
            if kind == "check":
                version.checks = [*version.checks, {**recorded, "reported_by": "client"}]
            else:
                version.evaluation = [*version.evaluation, recorded]
            return _receipt(version)

    def set_default(self, tenant, series_id, *, author, template_id, version_id, revision, reason, request_id):
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("INVALID_ARGUMENT: Give a reason for the default choice.")
        digest = _hash([version_id, revision, reason])
        key = _hash([author, request_id])
        with self._write() as session:
            template = _template(session, tenant, series_id, template_id, lock=True)
            version = _version(session, template, version_id)
            prior = template.default_requests.get(key)
            if prior:
                _same_request(prior, digest)
            else:
                if revision != template.revision:
                    raise ValueError("TEMPLATE_CONFLICT: Read the current revision before selecting a default.")
                template.default_version_id = version.id
                template.revision += 1
                template.default_requests = {**template.default_requests, key: digest}
                version.evaluation = [*version.evaluation, {"kind": "default_selection", "context": reason,
                                       "author": author, "created_at": now_unix_time_ms(), "about_version_id": version.id}]
            return {**_receipt(version), "revision": revision + 1, "default_version_id": version.id}
