"""Local watch conversations use the same agent records and image store as the Web app."""
from contextvars import ContextVar
from pathlib import Path
from time import time_ns
from uuid import uuid4

from sqlalchemy import select

from server.constants.identity import LOCAL_USER_ID
from server.constants.tenant import DEFAULT_LOCAL_TENANT_ID as TENANT
from server.model.agent import AgentConversation, AgentMessage
from server.repository.agent import AgentRepository
from server.service.agent_attachments import AgentAttachmentService


LIBRARY_CHAT = "@library"
watch_updates = ContextVar("watch_updates", default=None)


def now_ms():
    return time_ns() // 1_000_000


class WatchHistory:
    def __init__(self, sessions, artifacts, images):
        self.sessions = sessions
        self.repo = AgentRepository(sessions)
        self.images = AgentAttachmentService(self.repo, artifacts, config=images)
        self.artifacts = artifacts
        self.max_bytes = images.maximum_upload_size_bytes

    def recover(self):
        with self.sessions() as session:
            interrupted = session.scalars(select(AgentMessage.id).join(
                AgentConversation, AgentMessage.conversation_id == AgentConversation.id,
            ).where(AgentConversation.idempotency_key.startswith("desktop-watch/"),
                    AgentConversation.tenant_id == TENANT, AgentConversation.owner_id == LOCAL_USER_ID,
                    AgentMessage.status == "PROCESSING")).all()
        for message in interrupted:
            self.fail(message, "上次回答因应用关闭而中断。")

        self.images.reconcile_deletions()

    def threads(self, video):
        with self.sessions() as session:
            rows = session.execute(select(AgentConversation.id, AgentConversation.title).where(
                AgentConversation.idempotency_key.startswith(f"desktop-watch/{video}/", autoescape=True),
                AgentConversation.tenant_id == TENANT, AgentConversation.owner_id == LOCAL_USER_ID,
            ).order_by(AgentConversation.last_message_time.desc(), AgentConversation.id.desc())).all()
        return [{"id": identifier, "title": title} for identifier, title in rows]

    def view(self, video, thread=None):
        threads = self.threads(video)
        if thread is None:
            thread = threads[0]["id"] if threads else ""
        if thread and not any(t["id"] == thread for t in threads):
            raise ValueError("此对话不属于当前视频。")
        rows = self.repo.list_messages(thread, TENANT, after_sequence_no=-1, limit=None) if thread else []
        messages = []
        context = {}
        for row in rows:
            if row.role == "USER":
                context = row.extra_context or {}
            failed = row.status != "COMPLETED"
            attachments = []
            for item in row.attachments:
                try:
                    stored = self.artifacts.read_conversation_attachment(thread, item["mediaUri"])
                    url = self.artifacts.resolve(stored.uri).as_uri()
                except (ValueError, OSError):
                    url = ""
                attachments.append({"mediaUri": item["mediaUri"], "previewUrl": url})
            messages.append({"role": row.role.lower(), "text": row.content or (
                row.last_error.message if row.last_error else "正在回答…"), "positionMs": context.get("positionMs", 0),
                "sourceRef": context.get("sourceRef", ""), "sources": context.get("sources", []), "failed": failed, "attachments": attachments})
        return {"threadID": thread, "threads": threads, "messages": messages}

    def create(self, video, title):
        uid = str(uuid4())
        self.repo.create_conversation(conversation_id=uid, tenant_id=TENANT, title=title[:80],
                                      idempotency_key=f"desktop-watch/{video}/{uid}")
        return uid

    def delete(self, video, thread):
        if not thread:
            raise ValueError("请先选择对话。")
        self.view(video, thread)
        self.repo.delete_conversation(thread, TENANT)
        self.images.delete_conversation_artifacts(thread)
        return self.view(video)

    def start(self, video, series, thread, question, position, reference, path, model, *, sources=()):
        if thread:
            self.view(video, thread)
        else:
            thread = self.create(video, question)
        attachments = []
        attachment_path = None
        if path:
            file = Path(path)
            if not file.is_file() or file.stat().st_size > self.max_bytes:
                raise ValueError("参考图片不可读或超过大小限制。")
            with file.open("rb") as stream:
                data = stream.read(self.max_bytes + 1)
            image = self.images.upload(thread, data, tenant_id=TENANT)
            attachments = [{"mediaUri": image.media_uri}]
            attachment_path = self.artifacts.resolve(image.media_uri)
        assistant = str(uuid4())
        self.repo.start_message(conversation_id=thread, tenant_id=TENANT, content=question,
            model_name=model, requested_top_k=1, requested_run_options={}, idempotency_key=str(uuid4()),
            series_ids=series, requested_series_ids=series, user_message_id=str(uuid4()),
            assistant_message_id=assistant, now=now_ms(), attachments=attachments,
            extra_context={"videoID": video, "positionMs": position, "sourceRef": reference, "sources": list(sources)})
        return thread, assistant, attachment_path

    def checkpoint(self, message, text):
        self.repo.checkpoint_assistant_message(message, content=text, now=now_ms())

    def finish(self, message, answer):
        self.repo.complete_assistant_message(message, content=answer, search_result=None, now=now_ms())

    def fail(self, message, error):
        self.repo.fail_assistant_message(message, error=error, public_message=error, now=now_ms())
