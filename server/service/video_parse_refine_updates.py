"""Process-local wakeups; persisted content versions remain authoritative."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager


class RefineSubscription:
    """One execution's updates, accessed only on its runtime's event loop."""

    def __init__(self, video_task_id: str, interval_id: str, members: Sequence[str]) -> None:
        self.video_task_id = video_task_id
        self.interval_id = interval_id
        self.members = frozenset(members)
        self.generation = 0
        self._versions: dict[str, int] = {}
        self._changed = asyncio.Event()

    def publish(self, versions: Mapping[str, int]) -> None:
        for member in self.members.intersection(versions):
            if versions[member] > self._versions.get(member, 0):
                self._versions[member] = versions[member]
                self._changed.set()

    def changes_since(self, versions: Mapping[str, int]) -> dict[str, int]:
        return {member: version for member, version in self._versions.items()
                if version > versions.get(member, 0)}

    async def wait(self, versions: Mapping[str, int]) -> dict[str, int]:
        while True:
            changes = self.changes_since(versions)
            if changes:
                return changes
            # No await between checking versions and clearing the event.
            self._changed.clear()
            await self._changed.wait()


class RefineContentUpdates:
    def __init__(self) -> None:
        self._subscriptions: set[RefineSubscription] = set()

    @contextmanager
    def subscribe(self, video_task_id: str, interval_id: str,
                  members: Sequence[str]) -> Iterator[RefineSubscription]:
        subscription = RefineSubscription(video_task_id, interval_id, members)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.remove(subscription)

    def publish(self, video_task_id: str, interval_id: str,
                versions: Mapping[str, int]) -> None:
        for subscription in self._subscriptions:
            if (subscription.video_task_id == video_task_id
                    and subscription.interval_id != interval_id):
                subscription.publish(versions)
