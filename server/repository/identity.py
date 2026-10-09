from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..constants.identity import LOCAL_USER_ID
from ..error.auth import IdentityConflictError, IdentityDeniedError
from ..model.agent import AgentConversation
from ..model.tenant import Tenant
from ..model.user import User


class IdentityRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def bootstrap(self, *, local_tenant_id: str, public_tenant_id: str, mode: str) -> None:
        with self._session_factory.begin() as session:
            if mode == "local":
                local = session.get(User, LOCAL_USER_ID)
                if local is None:
                    session.add(User(user_id=LOCAL_USER_ID, email=None, is_guest=False, role="admin"))
                    session.flush()
                elif local.is_guest or local.role != "admin" or local.status != "ACTIVE":
                    raise IdentityConflictError("local identity has incompatible state")
            fixed_tenants: list[tuple[str, str, str | None]] = [(public_tenant_id, "public", None)]
            if mode == "local":
                fixed_tenants.append((local_tenant_id, "local", LOCAL_USER_ID))
            for tenant_id, kind, owner in fixed_tenants:
                existing = session.get(Tenant, tenant_id)
                if existing is None:
                    session.add(Tenant(tenant_id=tenant_id, type=kind, owner_user_id=owner, name=tenant_id))
                elif existing.type != kind or existing.owner_user_id != owner or existing.status != "ACTIVE":
                    raise IdentityConflictError("fixed tenant has incompatible ownership")

    def get_user(self, user_id: str) -> User | None:
        with self._session_factory() as session:
            return session.get(User, user_id)

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._session_factory() as session:
            return session.get(Tenant, tenant_id)

    def get_owned_tenant(self, user_id: str) -> Tenant | None:
        with self._session_factory() as session:
            return session.scalar(select(Tenant).where(Tenant.owner_user_id == user_id, Tenant.type == "personal", Tenant.status == "ACTIVE"))

    def create_guest(self) -> User:
        with self._session_factory.begin() as session:
            user = User(user_id=str(uuid4()), email=None, is_guest=True)
            session.add(user)
            session.flush()
            return user

    def login_email(self, *, guest_id: str, expected_version: int, email: str, role: str = "user") -> User:
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    guest = session.scalar(select(User).where(User.user_id == guest_id).with_for_update())
                    if guest is None or guest.status != "ACTIVE" or not guest.is_guest or guest.auth_version != expected_version:
                        raise IdentityDeniedError("guest credential is no longer valid")
                    target = session.scalar(select(User).where(User.email == email).with_for_update())
                    if target is None:
                        target = guest
                        guest.email, guest.is_guest, guest.role = email, False, role
                    elif target.status != "ACTIVE" or target.is_guest:
                        raise IdentityDeniedError("account is unavailable")
                    guest.auth_version += 1
                    if target is not guest:
                        guest.status = "DISABLED"
                        self._transfer_conversations(session, guest_id, target.user_id)
                    tenant = session.scalar(select(Tenant).where(Tenant.owner_user_id == target.user_id, Tenant.type == "personal"))
                    if tenant is None:
                        session.add(Tenant(tenant_id=str(uuid4()), owner_user_id=target.user_id, type="personal", name="个人资料"))
                    elif tenant.status != "ACTIVE":
                        raise IdentityDeniedError("personal tenant is unavailable")
                    session.flush()
                    return target
            except IntegrityError:
                if attempt == 1:
                    raise IdentityConflictError("account creation conflicted") from None
        raise AssertionError("unreachable")

    @staticmethod
    def _transfer_conversations(session: Session, guest_id: str, user_id: str) -> None:
        existing_keys = set(session.execute(select(
            AgentConversation.tenant_id, AgentConversation.idempotency_key,
        ).where(AgentConversation.owner_user_id == user_id)).all())
        conversations = session.scalars(select(AgentConversation).where(
            AgentConversation.owner_user_id == guest_id,
        ).with_for_update()).all()
        for conversation in conversations:
            scope_key = (conversation.tenant_id, conversation.idempotency_key)
            if scope_key in existing_keys:
                # Preserve both conversation IDs and their messages when the
                # guest and existing account used the same creation request key.
                conversation.idempotency_key = f"guest-transfer:{uuid4()}"
            conversation.owner_user_id = user_id
            existing_keys.add((conversation.tenant_id, conversation.idempotency_key))

    def revoke(self, user_id: str, expected_version: int) -> None:
        with self._session_factory.begin() as session:
            self._revoke(session, user_id, expected_version)

    def revoke_and_create_guest(self, user_id: str, expected_version: int) -> User:
        with self._session_factory.begin() as session:
            self._revoke(session, user_id, expected_version)
            guest = User(user_id=str(uuid4()), email=None, is_guest=True)
            session.add(guest)
            session.flush()
            return guest

    @staticmethod
    def _revoke(session: Session, user_id: str, expected_version: int) -> None:
        changed = session.execute(update(User).where(User.user_id == user_id, User.auth_version == expected_version, User.status == "ACTIVE").values(auth_version=User.auth_version + 1)).rowcount
        if changed != 1:
            raise IdentityDeniedError("credential is no longer valid")
