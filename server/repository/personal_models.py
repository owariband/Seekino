from collections.abc import Callable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.personal_models import PersonalModelPreferences

Preferences = tuple[list[dict], str]


class PersonalModelConflictError(Exception):
    pass


class PersonalModelRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def read(self, owner: str) -> Preferences:
        with self.sessions() as session:
            row = session.get(PersonalModelPreferences, owner)
            return (row.connections, row.default_model) if row else ([], "default")

    def update(self, owner: str, change: Callable[[list[dict], str], Preferences]) -> Preferences:
        # The row version protects simultaneous changes to different connections and the default.
        for _ in range(4):
            try:
                with self.sessions.begin() as session:
                    row = session.get(PersonalModelPreferences, owner)
                    connections, default = change(row.connections if row else [], row.default_model if row else "default")
                    if row is None:
                        session.add(PersonalModelPreferences(owner_id=owner, connections=connections, default_model=default, revision=1))
                        session.flush()
                    else:
                        saved = session.execute(update(PersonalModelPreferences).where(
                            PersonalModelPreferences.owner_id == owner,
                            PersonalModelPreferences.revision == row.revision,
                        ).values(connections=connections, default_model=default, revision=row.revision + 1))
                        if saved.rowcount != 1:
                            continue
                    return connections, default
            except IntegrityError:
                # Two initial writes can race on this owner's primary key.
                continue
        raise PersonalModelConflictError("模型设置已更改，请刷新后重试")
