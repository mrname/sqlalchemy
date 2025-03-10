# engine/default.py
# Copyright (C) 2005-2022 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: allow-untyped-defs, allow-untyped-calls

"""Default implementations of per-dialect sqlalchemy.engine classes.

These are semi-private implementation classes which are only of importance
to database dialect authors; dialects will usually use the classes here
as the base class for their own corresponding classes.

"""

from __future__ import annotations

import functools
import random
import re
from time import perf_counter
import typing
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import List
from typing import Mapping
from typing import MutableMapping
from typing import MutableSequence
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import Type
from typing import TYPE_CHECKING
import weakref

from . import characteristics
from . import cursor as _cursor
from . import interfaces
from .base import Connection
from .interfaces import CacheStats
from .interfaces import DBAPICursor
from .interfaces import Dialect
from .interfaces import ExecutionContext
from .. import event
from .. import exc
from .. import pool
from .. import util
from ..sql import compiler
from ..sql import expression
from ..sql import type_api
from ..sql._typing import is_tuple_type
from ..sql.compiler import DDLCompiler
from ..sql.compiler import SQLCompiler
from ..sql.elements import quoted_name
from ..sql.schema import default_is_scalar

if typing.TYPE_CHECKING:
    from types import ModuleType

    from .base import Engine
    from .interfaces import _CoreMultiExecuteParams
    from .interfaces import _CoreSingleExecuteParams
    from .interfaces import _DBAPICursorDescription
    from .interfaces import _DBAPIMultiExecuteParams
    from .interfaces import _ExecuteOptions
    from .interfaces import _IsolationLevel
    from .interfaces import _MutableCoreSingleExecuteParams
    from .interfaces import _ParamStyle
    from .interfaces import DBAPIConnection
    from .row import Row
    from .url import URL
    from ..event import _ListenerFnType
    from ..pool import Pool
    from ..pool import PoolProxiedConnection
    from ..sql import Executable
    from ..sql.compiler import Compiled
    from ..sql.compiler import Linting
    from ..sql.compiler import ResultColumnsEntry
    from ..sql.dml import DMLState
    from ..sql.dml import UpdateBase
    from ..sql.elements import BindParameter
    from ..sql.schema import Column
    from ..sql.type_api import _BindProcessorType
    from ..sql.type_api import TypeEngine

# When we're handed literal SQL, ensure it's a SELECT query
SERVER_SIDE_CURSOR_RE = re.compile(r"\s*SELECT", re.I | re.UNICODE)


(
    CACHE_HIT,
    CACHE_MISS,
    CACHING_DISABLED,
    NO_CACHE_KEY,
    NO_DIALECT_SUPPORT,
) = list(CacheStats)


class DefaultDialect(Dialect):
    """Default implementation of Dialect"""

    statement_compiler = compiler.SQLCompiler
    ddl_compiler = compiler.DDLCompiler
    type_compiler_cls = compiler.GenericTypeCompiler

    preparer = compiler.IdentifierPreparer
    supports_alter = True
    supports_comments = False
    inline_comments = False
    supports_statement_cache = True

    div_is_floordiv = True

    bind_typing = interfaces.BindTyping.NONE

    include_set_input_sizes: Optional[Set[Any]] = None
    exclude_set_input_sizes: Optional[Set[Any]] = None

    # the first value we'd get for an autoincrement
    # column.
    default_sequence_base = 1

    # most DBAPIs happy with this for execute().
    # not cx_oracle.
    execute_sequence_format = tuple  # type: ignore

    supports_schemas = True
    supports_views = True
    supports_sequences = False
    sequences_optional = False
    preexecute_autoincrement_sequences = False
    supports_identity_columns = False
    postfetch_lastrowid = True
    insert_null_pk_still_autoincrements = False
    implicit_returning = False
    full_returning = False
    insert_executemany_returning = False

    cte_follows_insert = False

    supports_native_enum = False
    supports_native_boolean = False
    non_native_boolean_check_constraint = True

    supports_simple_order_by_label = True

    tuple_in_values = False

    connection_characteristics = util.immutabledict(
        {"isolation_level": characteristics.IsolationLevelCharacteristic()}
    )

    engine_config_types: Mapping[str, Any] = util.immutabledict(
        {
            "pool_timeout": util.asint,
            "echo": util.bool_or_str("debug"),
            "echo_pool": util.bool_or_str("debug"),
            "pool_recycle": util.asint,
            "pool_size": util.asint,
            "max_overflow": util.asint,
            "future": util.asbool,
        }
    )

    # if the NUMERIC type
    # returns decimal.Decimal.
    # *not* the FLOAT type however.
    supports_native_decimal = False

    name = "default"

    # length at which to truncate
    # any identifier.
    max_identifier_length = 9999
    _user_defined_max_identifier_length: Optional[int] = None

    isolation_level: Optional[str] = None

    # sub-categories of max_identifier_length.
    # currently these accommodate for MySQL which allows alias names
    # of 255 but DDL names only of 64.
    max_index_name_length: Optional[int] = None
    max_constraint_name_length: Optional[int] = None

    supports_sane_rowcount = True
    supports_sane_multi_rowcount = True
    colspecs: MutableMapping[
        Type["TypeEngine[Any]"], Type["TypeEngine[Any]"]
    ] = {}
    default_paramstyle = "named"

    supports_default_values = False
    """dialect supports INSERT... DEFAULT VALUES syntax"""

    supports_default_metavalue = False
    """dialect supports INSERT... VALUES (DEFAULT) syntax"""

    # not sure if this is a real thing but the compiler will deliver it
    # if this is the only flag enabled.
    supports_empty_insert = True
    """dialect supports INSERT () VALUES ()"""

    supports_multivalues_insert = False

    supports_is_distinct_from = True

    supports_server_side_cursors = False

    server_side_cursors = False

    # extra record-level locking features (#4860)
    supports_for_update_of = False

    server_version_info = None

    default_schema_name: Optional[str] = None

    # indicates symbol names are
    # UPPERCASEd if they are case insensitive
    # within the database.
    # if this is True, the methods normalize_name()
    # and denormalize_name() must be provided.
    requires_name_normalize = False

    is_async = False

    # TODO: this is not to be part of 2.0.  implement rudimentary binary
    # literals for SQLite, PostgreSQL, MySQL only within
    # _Binary.literal_processor
    _legacy_binary_type_literal_encoding = "utf-8"

    @util.deprecated_params(
        empty_in_strategy=(
            "1.4",
            "The :paramref:`_sa.create_engine.empty_in_strategy` keyword is "
            "deprecated, and no longer has any effect.  All IN expressions "
            "are now rendered using "
            'the "expanding parameter" strategy which renders a set of bound'
            'expressions, or an "empty set" SELECT, at statement execution'
            "time.",
        ),
        server_side_cursors=(
            "1.4",
            "The :paramref:`_sa.create_engine.server_side_cursors` parameter "
            "is deprecated and will be removed in a future release.  Please "
            "use the "
            ":paramref:`_engine.Connection.execution_options.stream_results` "
            "parameter.",
        ),
    )
    def __init__(
        self,
        paramstyle: Optional[_ParamStyle] = None,
        isolation_level: Optional[_IsolationLevel] = None,
        dbapi: Optional[ModuleType] = None,
        implicit_returning: Optional[bool] = None,
        supports_native_boolean: Optional[bool] = None,
        max_identifier_length: Optional[int] = None,
        label_length: Optional[int] = None,
        # util.deprecated_params decorator cannot render the
        # Linting.NO_LINTING constant
        compiler_linting: Linting = int(compiler.NO_LINTING),  # type: ignore
        server_side_cursors: bool = False,
        **kwargs: Any,
    ):
        if server_side_cursors:
            if not self.supports_server_side_cursors:
                raise exc.ArgumentError(
                    "Dialect %s does not support server side cursors" % self
                )
            else:
                self.server_side_cursors = True

        if getattr(self, "use_setinputsizes", False):
            util.warn_deprecated(
                "The dialect-level use_setinputsizes attribute is "
                "deprecated.  Please use "
                "bind_typing = BindTyping.SETINPUTSIZES",
                "2.0",
            )
            self.bind_typing = interfaces.BindTyping.SETINPUTSIZES

        self.positional = False
        self._ischema = None

        self.dbapi = dbapi

        if paramstyle is not None:
            self.paramstyle = paramstyle
        elif self.dbapi is not None:
            self.paramstyle = self.dbapi.paramstyle
        else:
            self.paramstyle = self.default_paramstyle
        if implicit_returning is not None:
            self.implicit_returning = implicit_returning
        self.positional = self.paramstyle in ("qmark", "format", "numeric")
        self.identifier_preparer = self.preparer(self)
        self._on_connect_isolation_level = isolation_level

        legacy_tt_callable = getattr(self, "type_compiler", None)
        if legacy_tt_callable is not None:
            tt_callable = cast(
                Type[compiler.GenericTypeCompiler],
                self.type_compiler,
            )
        else:
            tt_callable = self.type_compiler_cls

        self.type_compiler_instance = self.type_compiler = tt_callable(self)

        if supports_native_boolean is not None:
            self.supports_native_boolean = supports_native_boolean

        self._user_defined_max_identifier_length = max_identifier_length
        if self._user_defined_max_identifier_length:
            self.max_identifier_length = (
                self._user_defined_max_identifier_length
            )
        self.label_length = label_length
        self.compiler_linting = compiler_linting

    @util.memoized_property
    def loaded_dbapi(self) -> ModuleType:
        if self.dbapi is None:
            raise exc.InvalidRequestError(
                f"Dialect {self} does not have a Python DBAPI established "
                "and cannot be used for actual database interaction"
            )
        return self.dbapi

    @util.memoized_property
    def _bind_typing_render_casts(self):
        return self.bind_typing is interfaces.BindTyping.RENDER_CASTS

    def _ensure_has_table_connection(self, arg):

        if not isinstance(arg, Connection):
            raise exc.ArgumentError(
                "The argument passed to Dialect.has_table() should be a "
                "%s, got %s. "
                "Additionally, the Dialect.has_table() method is for "
                "internal dialect "
                "use only; please use "
                "``inspect(some_engine).has_table(<tablename>>)`` "
                "for public API use." % (Connection, type(arg))
            )

    @util.memoized_property
    def _supports_statement_cache(self):
        ssc = self.__class__.__dict__.get("supports_statement_cache", None)
        if ssc is None:
            util.warn(
                "Dialect %s:%s will not make use of SQL compilation caching "
                "as it does not set the 'supports_statement_cache' attribute "
                "to ``True``.  This can have "
                "significant performance implications including some "
                "performance degradations in comparison to prior SQLAlchemy "
                "versions.  Dialect maintainers should seek to set this "
                "attribute to True after appropriate development and testing "
                "for SQLAlchemy 1.4 caching support.   Alternatively, this "
                "attribute may be set to False which will disable this "
                "warning." % (self.name, self.driver),
                code="cprf",
            )

        return bool(ssc)

    @util.memoized_property
    def _type_memos(self):
        return weakref.WeakKeyDictionary()

    @property
    def dialect_description(self):
        return self.name + "+" + self.driver

    @property
    def supports_sane_rowcount_returning(self):
        """True if this dialect supports sane rowcount even if RETURNING is
        in use.

        For dialects that don't support RETURNING, this is synonymous with
        ``supports_sane_rowcount``.

        """
        return self.supports_sane_rowcount

    @classmethod
    def get_pool_class(cls, url: URL) -> Type[Pool]:
        return getattr(cls, "poolclass", pool.QueuePool)

    def get_dialect_pool_class(self, url: URL) -> Type[Pool]:
        return self.get_pool_class(url)

    @classmethod
    def load_provisioning(cls):
        package = ".".join(cls.__module__.split(".")[0:-1])
        try:
            __import__(package + ".provision")
        except ImportError:
            pass

    def _builtin_onconnect(self) -> Optional[_ListenerFnType]:
        if self._on_connect_isolation_level is not None:

            def builtin_connect(dbapi_conn, conn_rec):
                self._assert_and_set_isolation_level(
                    dbapi_conn, self._on_connect_isolation_level
                )

            return builtin_connect
        else:
            return None

    def initialize(self, connection):
        try:
            self.server_version_info = self._get_server_version_info(
                connection
            )
        except NotImplementedError:
            self.server_version_info = None
        try:
            self.default_schema_name = self._get_default_schema_name(
                connection
            )
        except NotImplementedError:
            self.default_schema_name = None

        try:
            self.default_isolation_level = self.get_default_isolation_level(
                connection.connection.dbapi_connection
            )
        except NotImplementedError:
            self.default_isolation_level = None

        if not self._user_defined_max_identifier_length:
            max_ident_length = self._check_max_identifier_length(connection)
            if max_ident_length:
                self.max_identifier_length = max_ident_length

        if (
            self.label_length
            and self.label_length > self.max_identifier_length
        ):
            raise exc.ArgumentError(
                "Label length of %d is greater than this dialect's"
                " maximum identifier length of %d"
                % (self.label_length, self.max_identifier_length)
            )

    def on_connect(self):
        # inherits the docstring from interfaces.Dialect.on_connect
        return None

    def _check_max_identifier_length(self, connection):
        """Perform a connection / server version specific check to determine
        the max_identifier_length.

        If the dialect's class level max_identifier_length should be used,
        can return None.

        .. versionadded:: 1.3.9

        """
        return None

    def get_default_isolation_level(self, dbapi_conn):
        """Given a DBAPI connection, return its isolation level, or
        a default isolation level if one cannot be retrieved.

        May be overridden by subclasses in order to provide a
        "fallback" isolation level for databases that cannot reliably
        retrieve the actual isolation level.

        By default, calls the :meth:`_engine.Interfaces.get_isolation_level`
        method, propagating any exceptions raised.

        .. versionadded:: 1.3.22

        """
        return self.get_isolation_level(dbapi_conn)

    def type_descriptor(self, typeobj):
        """Provide a database-specific :class:`.TypeEngine` object, given
        the generic object which comes from the types module.

        This method looks for a dictionary called
        ``colspecs`` as a class or instance-level variable,
        and passes on to :func:`_types.adapt_type`.

        """
        return type_api.adapt_type(typeobj, self.colspecs)

    def has_index(self, connection, table_name, index_name, schema=None):
        if not self.has_table(connection, table_name, schema=schema):
            return False
        for idx in self.get_indexes(connection, table_name, schema=schema):
            if idx["name"] == index_name:
                return True
        else:
            return False

    def validate_identifier(self, ident):
        if len(ident) > self.max_identifier_length:
            raise exc.IdentifierError(
                "Identifier '%s' exceeds maximum length of %d characters"
                % (ident, self.max_identifier_length)
            )

    def connect(self, *cargs, **cparams):
        # inherits the docstring from interfaces.Dialect.connect
        return self.loaded_dbapi.connect(*cargs, **cparams)

    def create_connect_args(self, url):
        # inherits the docstring from interfaces.Dialect.create_connect_args
        opts = url.translate_connect_args()
        opts.update(url.query)
        return [[], opts]

    def set_engine_execution_options(
        self, engine: Engine, opts: Mapping[str, str]
    ) -> None:
        supported_names = set(self.connection_characteristics).intersection(
            opts
        )
        if supported_names:
            characteristics: Mapping[str, str] = util.immutabledict(
                (name, opts[name]) for name in supported_names
            )

            @event.listens_for(engine, "engine_connect")
            def set_connection_characteristics(connection):
                self._set_connection_characteristics(
                    connection, characteristics
                )

    def set_connection_execution_options(
        self, connection: Connection, opts: Mapping[str, str]
    ) -> None:
        supported_names = set(self.connection_characteristics).intersection(
            opts
        )
        if supported_names:
            characteristics: Mapping[str, str] = util.immutabledict(
                (name, opts[name]) for name in supported_names
            )
            self._set_connection_characteristics(connection, characteristics)

    def _set_connection_characteristics(self, connection, characteristics):

        characteristic_values = [
            (name, self.connection_characteristics[name], value)
            for name, value in characteristics.items()
        ]

        if connection.in_transaction():
            trans_objs = [
                (name, obj)
                for name, obj, value in characteristic_values
                if obj.transactional
            ]
            if trans_objs:
                raise exc.InvalidRequestError(
                    "This connection has already initialized a SQLAlchemy "
                    "Transaction() object via begin() or autobegin; "
                    "%s may not be altered unless rollback() or commit() "
                    "is called first."
                    % (", ".join(name for name, obj in trans_objs))
                )

        dbapi_connection = connection.connection.dbapi_connection
        for name, characteristic, value in characteristic_values:
            characteristic.set_characteristic(self, dbapi_connection, value)
        connection.connection._connection_record.finalize_callback.append(
            functools.partial(self._reset_characteristics, characteristics)
        )

    def _reset_characteristics(self, characteristics, dbapi_connection):
        for characteristic_name in characteristics:
            characteristic = self.connection_characteristics[
                characteristic_name
            ]
            characteristic.reset_characteristic(self, dbapi_connection)

    def do_begin(self, dbapi_connection):
        pass

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()

    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    def do_close(self, dbapi_connection):
        dbapi_connection.close()

    @util.memoized_property
    def _dialect_specific_select_one(self):
        return str(expression.select(1).compile(dialect=self))

    def do_ping(self, dbapi_connection: DBAPIConnection) -> bool:
        cursor = None
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(self._dialect_specific_select_one)
            finally:
                cursor.close()
        except self.loaded_dbapi.Error as err:
            if self.is_disconnect(err, dbapi_connection, cursor):
                return False
            else:
                raise
        else:
            return True

    def create_xid(self):
        """Create a random two-phase transaction ID.

        This id will be passed to do_begin_twophase(), do_rollback_twophase(),
        do_commit_twophase().  Its format is unspecified.
        """

        return "_sa_%032x" % random.randint(0, 2**128)

    def do_savepoint(self, connection, name):
        connection.execute(expression.SavepointClause(name))

    def do_rollback_to_savepoint(self, connection, name):
        connection.execute(expression.RollbackToSavepointClause(name))

    def do_release_savepoint(self, connection, name):
        connection.execute(expression.ReleaseSavepointClause(name))

    def do_executemany(self, cursor, statement, parameters, context=None):
        cursor.executemany(statement, parameters)

    def do_execute(self, cursor, statement, parameters, context=None):
        cursor.execute(statement, parameters)

    def do_execute_no_params(self, cursor, statement, context=None):
        cursor.execute(statement)

    def is_disconnect(self, e, connection, cursor):
        return False

    @util.memoized_instancemethod
    def _gen_allowed_isolation_levels(self, dbapi_conn):

        try:
            raw_levels = list(self.get_isolation_level_values(dbapi_conn))
        except NotImplementedError:
            return None
        else:
            normalized_levels = [
                level.replace("_", " ").upper() for level in raw_levels
            ]
            if raw_levels != normalized_levels:
                raise ValueError(
                    f"Dialect {self.name!r} get_isolation_level_values() "
                    f"method should return names as UPPERCASE using spaces, "
                    f"not underscores; got "
                    f"{sorted(set(raw_levels).difference(normalized_levels))}"
                )
            return tuple(normalized_levels)

    def _assert_and_set_isolation_level(self, dbapi_conn, level):
        level = level.replace("_", " ").upper()

        _allowed_isolation_levels = self._gen_allowed_isolation_levels(
            dbapi_conn
        )
        if (
            _allowed_isolation_levels
            and level not in _allowed_isolation_levels
        ):
            raise exc.ArgumentError(
                f"Invalid value {level!r} for isolation_level. "
                f"Valid isolation levels for {self.name!r} are "
                f"{', '.join(_allowed_isolation_levels)}"
            )

        self.set_isolation_level(dbapi_conn, level)

    def reset_isolation_level(self, dbapi_conn):
        # default_isolation_level is read from the first connection
        # after the initial set of 'isolation_level', if any, so is
        # the configured default of this dialect.
        self._assert_and_set_isolation_level(
            dbapi_conn, self.default_isolation_level
        )

    def normalize_name(self, name):
        if name is None:
            return None

        name_lower = name.lower()
        name_upper = name.upper()

        if name_upper == name_lower:
            # name has no upper/lower conversion, e.g. non-european characters.
            # return unchanged
            return name
        elif name_upper == name and not (
            self.identifier_preparer._requires_quotes
        )(name_lower):
            # name is all uppercase and doesn't require quoting; normalize
            # to all lower case
            return name_lower
        elif name_lower == name:
            # name is all lower case, which if denormalized means we need to
            # force quoting on it
            return quoted_name(name, quote=True)
        else:
            # name is mixed case, means it will be quoted in SQL when used
            # later, no normalizes
            return name

    def denormalize_name(self, name):
        if name is None:
            return None

        name_lower = name.lower()
        name_upper = name.upper()

        if name_upper == name_lower:
            # name has no upper/lower conversion, e.g. non-european characters.
            # return unchanged
            return name
        elif name_lower == name and not (
            self.identifier_preparer._requires_quotes
        )(name_lower):
            name = name_upper
        return name

    def get_driver_connection(self, connection):
        return connection


class StrCompileDialect(DefaultDialect):

    statement_compiler = compiler.StrSQLCompiler
    ddl_compiler = compiler.DDLCompiler
    type_compiler_cls = compiler.StrSQLTypeCompiler
    preparer = compiler.IdentifierPreparer

    supports_statement_cache = True

    supports_identity_columns = True

    supports_sequences = True
    sequences_optional = True
    preexecute_autoincrement_sequences = False
    implicit_returning = False

    supports_native_boolean = True

    supports_multivalues_insert = True
    supports_simple_order_by_label = True


class DefaultExecutionContext(ExecutionContext):
    isinsert = False
    isupdate = False
    isdelete = False
    is_crud = False
    is_text = False
    isddl = False

    executemany = False
    compiled: Optional[Compiled] = None
    result_column_struct: Optional[
        Tuple[List[ResultColumnsEntry], bool, bool, bool]
    ] = None
    returned_default_rows: Optional[Sequence[Row[Any]]] = None

    execution_options: _ExecuteOptions = util.EMPTY_DICT

    cursor_fetch_strategy = _cursor._DEFAULT_FETCH

    invoked_statement: Optional[Executable] = None

    _is_implicit_returning = False
    _is_explicit_returning = False
    _is_server_side = False

    _soft_closed = False

    # a hook for SQLite's translation of
    # result column names
    # NOTE: pyhive is using this hook, can't remove it :(
    _translate_colname: Optional[Callable[[str], str]] = None

    _expanded_parameters: Mapping[str, List[str]] = util.immutabledict()
    """used by set_input_sizes().

    This collection comes from ``ExpandedState.parameter_expansion``.

    """

    cache_hit = NO_CACHE_KEY

    root_connection: Connection
    _dbapi_connection: PoolProxiedConnection
    dialect: Dialect
    unicode_statement: str
    cursor: DBAPICursor
    compiled_parameters: List[_MutableCoreSingleExecuteParams]
    parameters: _DBAPIMultiExecuteParams
    extracted_parameters: Optional[Sequence[BindParameter[Any]]]

    _empty_dict_params = cast("Mapping[str, Any]", util.EMPTY_DICT)

    @classmethod
    def _init_ddl(
        cls,
        dialect: Dialect,
        connection: Connection,
        dbapi_connection: PoolProxiedConnection,
        execution_options: _ExecuteOptions,
        compiled_ddl: DDLCompiler,
    ) -> ExecutionContext:
        """Initialize execution context for an ExecutableDDLElement
        construct."""

        self = cls.__new__(cls)
        self.root_connection = connection
        self._dbapi_connection = dbapi_connection
        self.dialect = connection.dialect

        self.compiled = compiled = compiled_ddl
        self.isddl = True

        self.execution_options = execution_options

        self.unicode_statement = str(compiled)
        if compiled.schema_translate_map:
            schema_translate_map = self.execution_options.get(
                "schema_translate_map", {}
            )

            rst = compiled.preparer._render_schema_translates
            self.unicode_statement = rst(
                self.unicode_statement, schema_translate_map
            )

        self.statement = self.unicode_statement

        self.cursor = self.create_cursor()
        self.compiled_parameters = []

        if dialect.positional:
            self.parameters = [dialect.execute_sequence_format()]
        else:
            self.parameters = [self._empty_dict_params]

        return self

    @classmethod
    def _init_compiled(
        cls,
        dialect: Dialect,
        connection: Connection,
        dbapi_connection: PoolProxiedConnection,
        execution_options: _ExecuteOptions,
        compiled: SQLCompiler,
        parameters: _CoreMultiExecuteParams,
        invoked_statement: Executable,
        extracted_parameters: Optional[Sequence[BindParameter[Any]]],
        cache_hit: CacheStats = CacheStats.CACHING_DISABLED,
    ) -> ExecutionContext:
        """Initialize execution context for a Compiled construct."""

        self = cls.__new__(cls)
        self.root_connection = connection
        self._dbapi_connection = dbapi_connection
        self.dialect = connection.dialect
        self.extracted_parameters = extracted_parameters
        self.invoked_statement = invoked_statement
        self.compiled = compiled
        self.cache_hit = cache_hit

        self.execution_options = execution_options

        self.result_column_struct = (
            compiled._result_columns,
            compiled._ordered_columns,
            compiled._textual_ordered_columns,
            compiled._loose_column_name_matching,
        )

        self.isinsert = compiled.isinsert
        self.isupdate = compiled.isupdate
        self.isdelete = compiled.isdelete
        self.is_text = compiled.isplaintext

        if self.isinsert or self.isupdate or self.isdelete:
            if TYPE_CHECKING:
                assert isinstance(compiled.statement, UpdateBase)
            self.is_crud = True
            self._is_explicit_returning = bool(compiled.statement._returning)
            self._is_implicit_returning = is_implicit_returning = bool(
                compiled.implicit_returning
            )
            assert not (
                is_implicit_returning and compiled.statement._returning
            )

        if not parameters:
            self.compiled_parameters = [
                compiled.construct_params(
                    extracted_parameters=extracted_parameters
                )
            ]
        else:
            self.compiled_parameters = [
                compiled.construct_params(
                    m,
                    _group_number=grp,
                    extracted_parameters=extracted_parameters,
                )
                for grp, m in enumerate(parameters)
            ]

            self.executemany = len(parameters) > 1

        self.unicode_statement = compiled.string

        self.cursor = self.create_cursor()

        if self.compiled.insert_prefetch or self.compiled.update_prefetch:
            if self.executemany:
                self._process_executemany_defaults()
            else:
                self._process_executesingle_defaults()

        processors = compiled._bind_processors

        flattened_processors: Mapping[
            str, _BindProcessorType[Any]
        ] = processors  # type: ignore[assignment]

        if compiled.literal_execute_params or compiled.post_compile_params:
            if self.executemany:
                raise exc.InvalidRequestError(
                    "'literal_execute' or 'expanding' parameters can't be "
                    "used with executemany()"
                )

            expanded_state = compiled._process_parameters_for_postcompile(
                self.compiled_parameters[0]
            )

            # re-assign self.unicode_statement
            self.unicode_statement = expanded_state.statement

            self._expanded_parameters = expanded_state.parameter_expansion

            flattened_processors = dict(processors)  # type: ignore
            flattened_processors.update(expanded_state.processors)
            positiontup = expanded_state.positiontup
        elif compiled.positional:
            positiontup = self.compiled.positiontup
        else:
            positiontup = None

        if compiled.schema_translate_map:
            schema_translate_map = self.execution_options.get(
                "schema_translate_map", {}
            )
            rst = compiled.preparer._render_schema_translates
            self.unicode_statement = rst(
                self.unicode_statement, schema_translate_map
            )

        # final self.unicode_statement is now assigned, encode if needed
        # by dialect
        self.statement = self.unicode_statement

        # Convert the dictionary of bind parameter values
        # into a dict or list to be sent to the DBAPI's
        # execute() or executemany() method.

        if compiled.positional:
            core_positional_parameters: MutableSequence[Sequence[Any]] = []
            assert positiontup is not None
            for compiled_params in self.compiled_parameters:
                l_param: List[Any] = [
                    flattened_processors[key](compiled_params[key])
                    if key in flattened_processors
                    else compiled_params[key]
                    for key in positiontup
                ]
                core_positional_parameters.append(
                    dialect.execute_sequence_format(l_param)
                )

            self.parameters = core_positional_parameters
        else:
            core_dict_parameters: MutableSequence[Dict[str, Any]] = []
            for compiled_params in self.compiled_parameters:

                d_param: Dict[str, Any] = {
                    key: flattened_processors[key](compiled_params[key])
                    if key in flattened_processors
                    else compiled_params[key]
                    for key in compiled_params
                }

                core_dict_parameters.append(d_param)

            self.parameters = core_dict_parameters

        return self

    @classmethod
    def _init_statement(
        cls,
        dialect: Dialect,
        connection: Connection,
        dbapi_connection: PoolProxiedConnection,
        execution_options: _ExecuteOptions,
        statement: str,
        parameters: _DBAPIMultiExecuteParams,
    ) -> ExecutionContext:
        """Initialize execution context for a string SQL statement."""

        self = cls.__new__(cls)
        self.root_connection = connection
        self._dbapi_connection = dbapi_connection
        self.dialect = connection.dialect
        self.is_text = True

        self.execution_options = execution_options

        if not parameters:
            if self.dialect.positional:
                self.parameters = [dialect.execute_sequence_format()]
            else:
                self.parameters = [self._empty_dict_params]
        elif isinstance(parameters[0], dialect.execute_sequence_format):
            self.parameters = parameters
        elif isinstance(parameters[0], dict):
            self.parameters = parameters
        else:
            self.parameters = [
                dialect.execute_sequence_format(p) for p in parameters
            ]

        self.executemany = len(parameters) > 1

        self.statement = self.unicode_statement = statement

        self.cursor = self.create_cursor()
        return self

    @classmethod
    def _init_default(
        cls,
        dialect: Dialect,
        connection: Connection,
        dbapi_connection: PoolProxiedConnection,
        execution_options: _ExecuteOptions,
    ) -> ExecutionContext:
        """Initialize execution context for a ColumnDefault construct."""

        self = cls.__new__(cls)
        self.root_connection = connection
        self._dbapi_connection = dbapi_connection
        self.dialect = connection.dialect

        self.execution_options = execution_options

        self.cursor = self.create_cursor()
        return self

    def _get_cache_stats(self) -> str:
        if self.compiled is None:
            return "raw sql"

        now = perf_counter()

        ch = self.cache_hit

        gen_time = self.compiled._gen_time
        assert gen_time is not None

        if ch is NO_CACHE_KEY:
            return "no key %.5fs" % (now - gen_time,)
        elif ch is CACHE_HIT:
            return "cached since %.4gs ago" % (now - gen_time,)
        elif ch is CACHE_MISS:
            return "generated in %.5fs" % (now - gen_time,)
        elif ch is CACHING_DISABLED:
            return "caching disabled %.5fs" % (now - gen_time,)
        elif ch is NO_DIALECT_SUPPORT:
            return "dialect %s+%s does not support caching %.5fs" % (
                self.dialect.name,
                self.dialect.driver,
                now - gen_time,
            )
        else:
            return "unknown"

    @util.memoized_property
    def identifier_preparer(self):
        if self.compiled:
            return self.compiled.preparer
        elif "schema_translate_map" in self.execution_options:
            return self.dialect.identifier_preparer._with_schema_translate(
                self.execution_options["schema_translate_map"]
            )
        else:
            return self.dialect.identifier_preparer

    @util.memoized_property
    def engine(self):
        return self.root_connection.engine

    @util.memoized_property
    def postfetch_cols(self) -> Optional[Sequence[Column[Any]]]:
        if TYPE_CHECKING:
            assert isinstance(self.compiled, SQLCompiler)
        return self.compiled.postfetch

    @util.memoized_property
    def prefetch_cols(self) -> Optional[Sequence[Column[Any]]]:
        if TYPE_CHECKING:
            assert isinstance(self.compiled, SQLCompiler)
        if self.isinsert:
            return self.compiled.insert_prefetch
        elif self.isupdate:
            return self.compiled.update_prefetch
        else:
            return ()

    @util.memoized_property
    def no_parameters(self):
        return self.execution_options.get("no_parameters", False)

    def _execute_scalar(self, stmt, type_, parameters=None):
        """Execute a string statement on the current cursor, returning a
        scalar result.

        Used to fire off sequences, default phrases, and "select lastrowid"
        types of statements individually or in the context of a parent INSERT
        or UPDATE statement.

        """

        conn = self.root_connection

        if "schema_translate_map" in self.execution_options:
            schema_translate_map = self.execution_options.get(
                "schema_translate_map", {}
            )

            rst = self.identifier_preparer._render_schema_translates
            stmt = rst(stmt, schema_translate_map)

        if not parameters:
            if self.dialect.positional:
                parameters = self.dialect.execute_sequence_format()
            else:
                parameters = {}

        conn._cursor_execute(self.cursor, stmt, parameters, context=self)
        row = self.cursor.fetchone()
        if row is not None:
            r = row[0]
        else:
            r = None
        if type_ is not None:
            # apply type post processors to the result
            proc = type_._cached_result_processor(
                self.dialect, self.cursor.description[0][1]
            )
            if proc:
                return proc(r)
        return r

    @util.memoized_property
    def connection(self):
        return self.root_connection

    def _use_server_side_cursor(self):
        if not self.dialect.supports_server_side_cursors:
            return False

        if self.dialect.server_side_cursors:
            # this is deprecated
            use_server_side = self.execution_options.get(
                "stream_results", True
            ) and (
                (
                    self.compiled
                    and isinstance(
                        self.compiled.statement, expression.Selectable
                    )
                    or (
                        (
                            not self.compiled
                            or isinstance(
                                self.compiled.statement, expression.TextClause
                            )
                        )
                        and self.unicode_statement
                        and SERVER_SIDE_CURSOR_RE.match(self.unicode_statement)
                    )
                )
            )
        else:
            use_server_side = self.execution_options.get(
                "stream_results", False
            )

        return use_server_side

    def create_cursor(self):
        if (
            # inlining initial preference checks for SS cursors
            self.dialect.supports_server_side_cursors
            and (
                self.execution_options.get("stream_results", False)
                or (
                    self.dialect.server_side_cursors
                    and self._use_server_side_cursor()
                )
            )
        ):
            self._is_server_side = True
            return self.create_server_side_cursor()
        else:
            self._is_server_side = False
            return self.create_default_cursor()

    def create_default_cursor(self):
        return self._dbapi_connection.cursor()

    def create_server_side_cursor(self):
        raise NotImplementedError()

    def pre_exec(self):
        pass

    def get_out_parameter_values(self, names):
        raise NotImplementedError(
            "This dialect does not support OUT parameters"
        )

    def post_exec(self):
        pass

    def get_result_processor(self, type_, colname, coltype):
        """Return a 'result processor' for a given type as present in
        cursor.description.

        This has a default implementation that dialects can override
        for context-sensitive result type handling.

        """
        return type_._cached_result_processor(self.dialect, coltype)

    def get_lastrowid(self):
        """return self.cursor.lastrowid, or equivalent, after an INSERT.

        This may involve calling special cursor functions, issuing a new SELECT
        on the cursor (or a new one), or returning a stored value that was
        calculated within post_exec().

        This function will only be called for dialects which support "implicit"
        primary key generation, keep preexecute_autoincrement_sequences set to
        False, and when no explicit id value was bound to the statement.

        The function is called once for an INSERT statement that would need to
        return the last inserted primary key for those dialects that make use
        of the lastrowid concept.  In these cases, it is called directly after
        :meth:`.ExecutionContext.post_exec`.

        """
        return self.cursor.lastrowid

    def handle_dbapi_exception(self, e):
        pass

    @util.non_memoized_property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def supports_sane_rowcount(self):
        return self.dialect.supports_sane_rowcount

    def supports_sane_multi_rowcount(self):
        return self.dialect.supports_sane_multi_rowcount

    def _setup_result_proxy(self):
        if self.is_crud or self.is_text:
            result = self._setup_dml_or_text_result()
        else:
            strategy = self.cursor_fetch_strategy
            if self._is_server_side and strategy is _cursor._DEFAULT_FETCH:
                strategy = _cursor.BufferedRowCursorFetchStrategy(
                    self.cursor, self.execution_options
                )
            cursor_description: _DBAPICursorDescription = (
                strategy.alternate_cursor_description
                or self.cursor.description
            )
            if cursor_description is None:
                strategy = _cursor._NO_CURSOR_DQL

            result = _cursor.CursorResult(self, strategy, cursor_description)

        compiled = self.compiled

        if (
            compiled
            and not self.isddl
            and cast(SQLCompiler, compiled).has_out_parameters
        ):
            self._setup_out_parameters(result)

        self._soft_closed = result._soft_closed

        return result

    def _setup_out_parameters(self, result):
        compiled = cast(SQLCompiler, self.compiled)

        out_bindparams = [
            (param, name)
            for param, name in compiled.bind_names.items()
            if param.isoutparam
        ]
        out_parameters = {}

        for bindparam, raw_value in zip(
            [param for param, name in out_bindparams],
            self.get_out_parameter_values(
                [name for param, name in out_bindparams]
            ),
        ):

            type_ = bindparam.type
            impl_type = type_.dialect_impl(self.dialect)
            dbapi_type = impl_type.get_dbapi_type(self.dialect.loaded_dbapi)
            result_processor = impl_type.result_processor(
                self.dialect, dbapi_type
            )
            if result_processor is not None:
                raw_value = result_processor(raw_value)
            out_parameters[bindparam.key] = raw_value

        result.out_parameters = out_parameters

    def _setup_dml_or_text_result(self):
        compiled = cast(SQLCompiler, self.compiled)

        if self.isinsert:
            if compiled.postfetch_lastrowid:
                self.inserted_primary_key_rows = (
                    self._setup_ins_pk_from_lastrowid()
                )
            # else if not self._is_implicit_returning,
            # the default inserted_primary_key_rows accessor will
            # return an "empty" primary key collection when accessed.

        strategy = self.cursor_fetch_strategy
        if self._is_server_side and strategy is _cursor._DEFAULT_FETCH:
            strategy = _cursor.BufferedRowCursorFetchStrategy(
                self.cursor, self.execution_options
            )
        cursor_description = (
            strategy.alternate_cursor_description or self.cursor.description
        )
        if cursor_description is None:
            strategy = _cursor._NO_CURSOR_DML

        result: _cursor.CursorResult[Any] = _cursor.CursorResult(
            self, strategy, cursor_description
        )

        if self.isinsert:
            if self._is_implicit_returning:
                rows = result.all()

                self.returned_default_rows = rows

                self.inserted_primary_key_rows = (
                    self._setup_ins_pk_from_implicit_returning(result, rows)
                )

                # test that it has a cursor metadata that is accurate. the
                # first row will have been fetched and current assumptions
                # are that the result has only one row, until executemany()
                # support is added here.
                assert result._metadata.returns_rows
                result._soft_close()
            elif not self._is_explicit_returning:
                result._soft_close()

                # we assume here the result does not return any rows.
                # *usually*, this will be true.  However, some dialects
                # such as that of MSSQL/pyodbc need to SELECT a post fetch
                # function so this is not necessarily true.
                # assert not result.returns_rows

        elif self.isupdate and self._is_implicit_returning:
            # get rowcount
            # (which requires open cursor on some drivers)
            # we were not doing this in 1.4, however
            # test_rowcount -> test_update_rowcount_return_defaults
            # is testing this, and psycopg will no longer return
            # rowcount after cursor is closed.
            result.rowcount

            row = result.fetchone()
            if row is not None:
                self.returned_default_rows = [row]

            result._soft_close()

            # test that it has a cursor metadata that is accurate.
            # the rows have all been fetched however.
            assert result._metadata.returns_rows

        elif not result._metadata.returns_rows:
            # no results, get rowcount
            # (which requires open cursor on some drivers)
            result.rowcount
            result._soft_close()
        return result

    @util.memoized_property
    def inserted_primary_key_rows(self):
        # if no specific "get primary key" strategy was set up
        # during execution, return a "default" primary key based
        # on what's in the compiled_parameters and nothing else.
        return self._setup_ins_pk_from_empty()

    def _setup_ins_pk_from_lastrowid(self):
        getter = cast(
            SQLCompiler, self.compiled
        )._inserted_primary_key_from_lastrowid_getter

        lastrowid = self.get_lastrowid()
        return [getter(lastrowid, self.compiled_parameters[0])]

    def _setup_ins_pk_from_empty(self):
        getter = cast(
            SQLCompiler, self.compiled
        )._inserted_primary_key_from_lastrowid_getter
        return [getter(None, param) for param in self.compiled_parameters]

    def _setup_ins_pk_from_implicit_returning(self, result, rows):

        if not rows:
            return []

        getter = cast(
            SQLCompiler, self.compiled
        )._inserted_primary_key_from_returning_getter
        compiled_params = self.compiled_parameters

        return [
            getter(row, param) for row, param in zip(rows, compiled_params)
        ]

    def lastrow_has_defaults(self):
        return (self.isinsert or self.isupdate) and bool(
            cast(SQLCompiler, self.compiled).postfetch
        )

    def _set_input_sizes(self):
        """Given a cursor and ClauseParameters, call the appropriate
        style of ``setinputsizes()`` on the cursor, using DB-API types
        from the bind parameter's ``TypeEngine`` objects.

        This method only called by those dialects which set
        the :attr:`.Dialect.bind_typing` attribute to
        :attr:`.BindTyping.SETINPUTSIZES`.   cx_Oracle is the only DBAPI
        that requires setinputsizes(), pyodbc offers it as an option.

        Prior to SQLAlchemy 2.0, the setinputsizes() approach was also used
        for pg8000 and asyncpg, which has been changed to inline rendering
        of casts.

        """
        if self.isddl or self.is_text:
            return

        compiled = cast(SQLCompiler, self.compiled)

        inputsizes = compiled._get_set_input_sizes_lookup()

        if inputsizes is None:
            return

        dialect = self.dialect

        # all of the rest of this... cython?

        if dialect._has_events:
            inputsizes = dict(inputsizes)
            dialect.dispatch.do_setinputsizes(
                inputsizes, self.cursor, self.statement, self.parameters, self
            )

        if compiled.escaped_bind_names:
            escaped_bind_names = compiled.escaped_bind_names
        else:
            escaped_bind_names = None

        if dialect.positional:
            items = [
                (key, compiled.binds[key])
                for key in compiled.positiontup or ()
            ]
        else:
            items = [
                (key, bindparam)
                for bindparam, key in compiled.bind_names.items()
            ]

        generic_inputsizes: List[Tuple[str, Any, TypeEngine[Any]]] = []
        for key, bindparam in items:
            if bindparam in compiled.literal_execute_params:
                continue

            if key in self._expanded_parameters:
                if is_tuple_type(bindparam.type):
                    num = len(bindparam.type.types)
                    dbtypes = inputsizes[bindparam]
                    generic_inputsizes.extend(
                        (
                            (
                                escaped_bind_names.get(paramname, paramname)
                                if escaped_bind_names is not None
                                else paramname
                            ),
                            dbtypes[idx % num],
                            bindparam.type.types[idx % num],
                        )
                        for idx, paramname in enumerate(
                            self._expanded_parameters[key]
                        )
                    )
                else:
                    dbtype = inputsizes.get(bindparam, None)
                    generic_inputsizes.extend(
                        (
                            (
                                escaped_bind_names.get(paramname, paramname)
                                if escaped_bind_names is not None
                                else paramname
                            ),
                            dbtype,
                            bindparam.type,
                        )
                        for paramname in self._expanded_parameters[key]
                    )
            else:
                dbtype = inputsizes.get(bindparam, None)

                escaped_name = (
                    escaped_bind_names.get(key, key)
                    if escaped_bind_names is not None
                    else key
                )

                generic_inputsizes.append(
                    (escaped_name, dbtype, bindparam.type)
                )
        try:
            dialect.do_set_input_sizes(self.cursor, generic_inputsizes, self)
        except BaseException as e:
            self.root_connection._handle_dbapi_exception(
                e, None, None, None, self
            )

    def _exec_default(self, column, default, type_):
        if default.is_sequence:
            return self.fire_sequence(default, type_)
        elif default.is_callable:
            self.current_column = column
            return default.arg(self)
        elif default.is_clause_element:
            return self._exec_default_clause_element(column, default, type_)
        else:
            return default.arg

    def _exec_default_clause_element(self, column, default, type_):
        # execute a default that's a complete clause element.  Here, we have
        # to re-implement a miniature version of the compile->parameters->
        # cursor.execute() sequence, since we don't want to modify the state
        # of the connection  / result in progress or create new connection/
        # result objects etc.
        # .. versionchanged:: 1.4

        if not default._arg_is_typed:
            default_arg = expression.type_coerce(default.arg, type_)
        else:
            default_arg = default.arg
        compiled = expression.select(default_arg).compile(dialect=self.dialect)
        compiled_params = compiled.construct_params()
        processors = compiled._bind_processors
        if compiled.positional:
            parameters = self.dialect.execute_sequence_format(
                [
                    processors[key](compiled_params[key])  # type: ignore
                    if key in processors
                    else compiled_params[key]
                    for key in compiled.positiontup or ()
                ]
            )
        else:
            parameters = dict(
                (
                    key,
                    processors[key](compiled_params[key])  # type: ignore
                    if key in processors
                    else compiled_params[key],
                )
                for key in compiled_params
            )
        return self._execute_scalar(
            str(compiled), type_, parameters=parameters
        )

    current_parameters: Optional[_CoreSingleExecuteParams] = None
    """A dictionary of parameters applied to the current row.

    This attribute is only available in the context of a user-defined default
    generation function, e.g. as described at :ref:`context_default_functions`.
    It consists of a dictionary which includes entries for each column/value
    pair that is to be part of the INSERT or UPDATE statement. The keys of the
    dictionary will be the key value of each :class:`_schema.Column`,
    which is usually
    synonymous with the name.

    Note that the :attr:`.DefaultExecutionContext.current_parameters` attribute
    does not accommodate for the "multi-values" feature of the
    :meth:`_expression.Insert.values` method.  The
    :meth:`.DefaultExecutionContext.get_current_parameters` method should be
    preferred.

    .. seealso::

        :meth:`.DefaultExecutionContext.get_current_parameters`

        :ref:`context_default_functions`

    """

    def get_current_parameters(self, isolate_multiinsert_groups=True):
        """Return a dictionary of parameters applied to the current row.

        This method can only be used in the context of a user-defined default
        generation function, e.g. as described at
        :ref:`context_default_functions`. When invoked, a dictionary is
        returned which includes entries for each column/value pair that is part
        of the INSERT or UPDATE statement. The keys of the dictionary will be
        the key value of each :class:`_schema.Column`,
        which is usually synonymous
        with the name.

        :param isolate_multiinsert_groups=True: indicates that multi-valued
         INSERT constructs created using :meth:`_expression.Insert.values`
         should be
         handled by returning only the subset of parameters that are local
         to the current column default invocation.   When ``False``, the
         raw parameters of the statement are returned including the
         naming convention used in the case of multi-valued INSERT.

        .. versionadded:: 1.2  added
           :meth:`.DefaultExecutionContext.get_current_parameters`
           which provides more functionality over the existing
           :attr:`.DefaultExecutionContext.current_parameters`
           attribute.

        .. seealso::

            :attr:`.DefaultExecutionContext.current_parameters`

            :ref:`context_default_functions`

        """
        try:
            parameters = self.current_parameters
            column = self.current_column
        except AttributeError:
            raise exc.InvalidRequestError(
                "get_current_parameters() can only be invoked in the "
                "context of a Python side column default function"
            )
        else:
            assert column is not None
            assert parameters is not None
        compile_state = cast(
            "DMLState", cast(SQLCompiler, self.compiled).compile_state
        )
        assert compile_state is not None
        if (
            isolate_multiinsert_groups
            and self.isinsert
            and compile_state._has_multi_parameters
        ):
            if column._is_multiparam_column:
                index = column.index + 1  # type: ignore
                d = {column.original.key: parameters[column.key]}
            else:
                d = {column.key: parameters[column.key]}
                index = 0
            assert compile_state._dict_parameters is not None
            keys = compile_state._dict_parameters.keys()
            d.update(
                (key, parameters["%s_m%d" % (key, index)]) for key in keys
            )
            return d
        else:
            return parameters

    def get_insert_default(self, column):
        if column.default is None:
            return None
        else:
            return self._exec_default(column, column.default, column.type)

    def get_update_default(self, column):
        if column.onupdate is None:
            return None
        else:
            return self._exec_default(column, column.onupdate, column.type)

    def _process_executemany_defaults(self):
        compiled = cast(SQLCompiler, self.compiled)

        key_getter = compiled._within_exec_param_key_getter

        scalar_defaults: Dict[Column[Any], Any] = {}

        insert_prefetch = compiled.insert_prefetch
        update_prefetch = compiled.update_prefetch

        # pre-determine scalar Python-side defaults
        # to avoid many calls of get_insert_default()/
        # get_update_default()
        for c in insert_prefetch:
            if c.default and default_is_scalar(c.default):
                scalar_defaults[c] = c.default.arg

        for c in update_prefetch:
            if c.onupdate and default_is_scalar(c.onupdate):
                scalar_defaults[c] = c.onupdate.arg

        for param in self.compiled_parameters:
            self.current_parameters = param
            for c in insert_prefetch:
                if c in scalar_defaults:
                    val = scalar_defaults[c]
                else:
                    val = self.get_insert_default(c)
                if val is not None:
                    param[key_getter(c)] = val
            for c in update_prefetch:
                if c in scalar_defaults:
                    val = scalar_defaults[c]
                else:
                    val = self.get_update_default(c)
                if val is not None:
                    param[key_getter(c)] = val

        del self.current_parameters

    def _process_executesingle_defaults(self):
        compiled = cast(SQLCompiler, self.compiled)

        key_getter = compiled._within_exec_param_key_getter
        self.current_parameters = (
            compiled_parameters
        ) = self.compiled_parameters[0]

        for c in compiled.insert_prefetch:
            if c.default and default_is_scalar(c.default):
                val = c.default.arg
            else:
                val = self.get_insert_default(c)

            if val is not None:
                compiled_parameters[key_getter(c)] = val

        for c in compiled.update_prefetch:
            val = self.get_update_default(c)

            if val is not None:
                compiled_parameters[key_getter(c)] = val
        del self.current_parameters


DefaultDialect.execution_ctx_cls = DefaultExecutionContext
