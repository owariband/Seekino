from __future__ import annotations

from typing import Any

from sqlalchemy import JSON
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import operators
from sqlalchemy.types import TypeDecorator

from ..utils.json import convert_json_string_payload


class StringEncodedJSON(TypeDecorator[Any]):
    """Keep numeric payloads out of the database's JSON number parser.

    The column remains JSON, but the payload is a JSON string decoded by Python
    on read. A path limits encoding to a nested payload, preserving queryable
    metadata. Historical arrays/objects remain readable without a migration.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, string_path: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.string_path = string_path

    def coerce_compared_value(self, op: Any, value: Any) -> Any:
        if op in (operators.json_getitem_op, operators.json_path_getitem_op):
            return self.impl.coerce_compared_value(op, value)
        return self

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        return convert_json_string_payload(
            value, encode=True, string_path=self.string_path,
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return convert_json_string_payload(
            value, encode=False, string_path=self.string_path,
        )
