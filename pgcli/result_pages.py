"""Bounded cursor ownership and conservative query eligibility for the SQL viewer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
import threading

import sqlparse
from sqlparse import sql, tokens


def simple_relation(statement: str) -> tuple[str | None, str] | None:
    """Accept one ordinary table SELECT, without changing the user's SQL."""
    if '/*!' in statement or len(sqlparse.split(statement)) != 1:
        return None
    parsed = sqlparse.parse(statement)
    if not parsed or parsed[0].get_type() != 'SELECT':
        return None

    def has_function(group: Any) -> bool:
        return isinstance(group, sql.Function) or any(has_function(t) for t in getattr(group, 'tokens', []))

    if has_function(parsed[0]):
        return None
    forbidden = {
        'WITH',
        'INTO',
        'FOR',
        'FOR UPDATE',
        'FOR SHARE',
        'LOCK',
        'UNION',
        'INTERSECT',
        'EXCEPT',
        'GROUP BY',
        'HAVING',
        'DISTINCT',
        'PROCEDURE',
        'SQL_CALC_FOUND_ROWS',
    }
    flat = [t for t in parsed[0].flatten() if not t.is_whitespace and t.ttype not in tokens.Comment]
    if sum(t.ttype in tokens.Keyword.DML and t.normalized == 'SELECT' for t in flat) != 1:
        return None
    for token in flat:
        if token.ttype in tokens.Keyword and (
            ' '.join(token.normalized.split()) in forbidden or 'JOIN' in token.normalized or token.normalized.startswith(('LOCK ', 'FOR '))
        ):
            return None
        if token.ttype not in tokens.Literal.String and (token.value.startswith('@') or token.value == '::'):
            return None
    top = [t for t in parsed[0].tokens if not t.is_whitespace and t.ttype not in tokens.Comment]
    for index, token in enumerate(top):
        if token.ttype in tokens.Keyword and token.normalized == 'FROM' and index + 1 < len(top):
            relation = top[index + 1]
            if not isinstance(relation, sql.Identifier) or any(isinstance(t, sql.Parenthesis) for t in relation.tokens):
                return None
            name = relation.get_real_name()
            return (relation.get_parent_name(), name) if name else None
    return None


class PagedRows:
    """A live iterator with explicit cancellation; never eagerly exhaust it."""

    def __init__(
        self, iterator: Iterator[tuple], description: Any, close: Callable[[], None], cancel: Callable[[], None], *, live: bool
    ) -> None:
        self.iterator = iterator
        self.description = description or ()
        self.headers = [str(c[0]) for c in self.description]
        self.preview: list[tuple] = []
        self.live = live
        self.exhausted = False
        self.closed = False
        self.cancelled = False
        self._cancel_lock = threading.Lock()
        self._close = close
        self._cancel = cancel

    def __iter__(self) -> Iterator[tuple]:
        # Accidental formatter use must not drain an unbounded cursor.
        return iter(self.preview)

    def next_row(self) -> tuple:
        try:
            return next(self.iterator)
        except StopIteration:
            self.exhausted = True
            raise

    def cancel(self) -> None:
        with self._cancel_lock:
            if not self.live or self.exhausted or self.cancelled:
                return
            self.cancelled = True
        try:
            self._cancel()
        except Exception:
            with self._cancel_lock:
                self.cancelled = False
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.cancel()
        self._close()
        self.closed = True


def postgres_stream(executor: Any, statement: str) -> PagedRows | None:
    import itertools
    import psycopg

    relation = simple_relation(statement)
    connection = executor.conn
    if not relation or not connection.autocommit or connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        return None
    statement_tokens = [t for t in sqlparse.parse(statement)[0].tokens if not t.is_whitespace]
    index = next(i for i, t in enumerate(statement_tokens) if t.ttype in tokens.Keyword and t.normalized == 'FROM')
    identifier = statement_tokens[index + 1]
    parts = list(identifier.tokens)
    if identifier.get_alias():
        while parts and parts[-1].is_whitespace:
            parts.pop()
        parts.pop()  # The alias token (possibly a quoted Identifier).
        while parts and parts[-1].is_whitespace:
            parts.pop()
        if parts and parts[-1].ttype in tokens.Keyword and parts[-1].normalized == 'AS':
            parts.pop()
    qualified = ''.join(t.value for t in parts).strip()
    try:
        with connection.cursor() as meta:
            meta.execute(
                'SELECT relkind, relpersistence, relrowsecurity, relhasrules '
                'FROM pg_catalog.pg_class WHERE oid = pg_catalog.to_regclass(%s)',
                (qualified,),
            )
            row = meta.fetchone()
            if not row or row[0] not in ('r', 'p') or row[1] == 't' or row[2] or row[3]:
                return None
    except psycopg.Error:
        return None
    cursor = connection.cursor()
    iterator = cursor.stream(statement)
    try:
        first = next(iterator)
    except StopIteration:
        cursor.close()
        result = PagedRows(iter(()), (), lambda: None, lambda: None, live=False)
        result.exhausted = True
        return result

    def close() -> None:
        iterator.close()  # psycopg cancels and drains an unfinished stream.
        cursor.close()

    return PagedRows(itertools.chain([first], iterator), cursor.description, close, connection.cancel, live=True)
