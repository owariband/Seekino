LOCAL_USER_ID = "local-user"


def guest_editing_tenant(user_id: str) -> str:
    """An editing-only namespace, never a media or account tenant."""
    return f"guest:{user_id}"
