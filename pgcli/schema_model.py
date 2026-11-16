"""Display-independent metadata used by the schema explorer."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    datatype: str
    nullable: bool
    primary: bool = False
    default: str | None = None
    comment: str = ''


@dataclass(frozen=True)
class Index:
    name: str
    definition: str


@dataclass
class Structure:
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    definition: str = ''
    note: str = ''
