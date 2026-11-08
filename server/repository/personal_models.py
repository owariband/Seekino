from sqlalchemy.orm import Session, sessionmaker

from ..model.personal_models import PersonalModelPreferences


class PersonalModelRepository:
    """Read the choices saved by older versions for their original owner."""
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def read(self, owner: str) -> tuple[list[dict], str]:
        with self.sessions() as session:
            row = session.get(PersonalModelPreferences, owner)
            return (row.connections, row.default_model) if row else ([], "default")
