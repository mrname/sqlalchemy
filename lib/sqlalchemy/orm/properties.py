# orm/properties.py
# Copyright (C) 2005-2022 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""MapperProperty implementations.

This is a private module which defines the behavior of individual ORM-
mapped attributes.

"""

from __future__ import annotations

from typing import Any
from typing import cast
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Type
from typing import TYPE_CHECKING
from typing import TypeVar

from . import attributes
from . import strategy_options
from .descriptor_props import Composite
from .descriptor_props import ConcreteInheritedProperty
from .descriptor_props import Synonym
from .interfaces import _AttributeOptions
from .interfaces import _DEFAULT_ATTRIBUTE_OPTIONS
from .interfaces import _IntrospectsAnnotations
from .interfaces import _MapsColumns
from .interfaces import MapperProperty
from .interfaces import PropComparator
from .interfaces import StrategizedProperty
from .relationships import Relationship
from .util import _orm_full_deannotate
from .. import exc as sa_exc
from .. import ForeignKey
from .. import log
from .. import util
from ..sql import coercions
from ..sql import roles
from ..sql import sqltypes
from ..sql.base import _NoArg
from ..sql.elements import SQLCoreOperations
from ..sql.schema import Column
from ..sql.schema import SchemaConst
from ..util.typing import de_optionalize_union_types
from ..util.typing import de_stringify_annotation
from ..util.typing import is_fwd_ref
from ..util.typing import NoneType
from ..util.typing import Self

if TYPE_CHECKING:
    from ._typing import _IdentityKeyType
    from ._typing import _InstanceDict
    from ._typing import _ORMColumnExprArgument
    from ._typing import _RegistryType
    from .mapper import Mapper
    from .session import Session
    from .state import _InstallLoaderCallableProto
    from .state import InstanceState
    from ..sql._typing import _InfoType
    from ..sql.elements import ColumnElement
    from ..sql.elements import NamedColumn
    from ..sql.operators import OperatorType
    from ..util.typing import _AnnotationScanType
    from ..util.typing import RODescriptorReference

_T = TypeVar("_T", bound=Any)
_PT = TypeVar("_PT", bound=Any)
_NC = TypeVar("_NC", bound="NamedColumn[Any]")

__all__ = [
    "ColumnProperty",
    "Composite",
    "ConcreteInheritedProperty",
    "Relationship",
    "Synonym",
]


@log.class_logger
class ColumnProperty(
    _MapsColumns[_T],
    StrategizedProperty[_T],
    _IntrospectsAnnotations,
    log.Identified,
):
    """Describes an object attribute that corresponds to a table column.

    Public constructor is the :func:`_orm.column_property` function.

    """

    strategy_wildcard_key = strategy_options._COLUMN_TOKEN
    inherit_cache = True
    _links_to_entity = False

    columns: List[NamedColumn[Any]]
    _orig_columns: List[NamedColumn[Any]]

    _is_polymorphic_discriminator: bool

    _mapped_by_synonym: Optional[str]

    comparator_factory: Type[PropComparator[_T]]

    __slots__ = (
        "_orig_columns",
        "columns",
        "group",
        "deferred",
        "instrument",
        "comparator_factory",
        "active_history",
        "expire_on_flush",
        "_creation_order",
        "_is_polymorphic_discriminator",
        "_mapped_by_synonym",
        "_deferred_column_loader",
        "_raise_column_loader",
        "_renders_in_subqueries",
        "raiseload",
    )

    def __init__(
        self,
        column: _ORMColumnExprArgument[_T],
        *additional_columns: _ORMColumnExprArgument[Any],
        attribute_options: Optional[_AttributeOptions] = None,
        group: Optional[str] = None,
        deferred: bool = False,
        raiseload: bool = False,
        comparator_factory: Optional[Type[PropComparator[_T]]] = None,
        active_history: bool = False,
        expire_on_flush: bool = True,
        info: Optional[_InfoType] = None,
        doc: Optional[str] = None,
        _instrument: bool = True,
    ):
        super(ColumnProperty, self).__init__(
            attribute_options=attribute_options
        )
        columns = (column,) + additional_columns
        self._orig_columns = [
            coercions.expect(roles.LabeledColumnExprRole, c) for c in columns
        ]
        self.columns = [
            _orm_full_deannotate(
                coercions.expect(roles.LabeledColumnExprRole, c)
            )
            for c in columns
        ]
        self.group = group
        self.deferred = deferred
        self.raiseload = raiseload
        self.instrument = _instrument
        self.comparator_factory = (
            comparator_factory
            if comparator_factory is not None
            else self.__class__.Comparator
        )
        self.active_history = active_history
        self.expire_on_flush = expire_on_flush

        if info is not None:
            self.info.update(info)

        if doc is not None:
            self.doc = doc
        else:
            for col in reversed(self.columns):
                doc = getattr(col, "doc", None)
                if doc is not None:
                    self.doc = doc
                    break
            else:
                self.doc = None

        util.set_creation_order(self)

        self.strategy_key = (
            ("deferred", self.deferred),
            ("instrument", self.instrument),
        )
        if self.raiseload:
            self.strategy_key += (("raiseload", True),)

    def declarative_scan(
        self,
        registry: _RegistryType,
        cls: Type[Any],
        key: str,
        annotation: Optional[_AnnotationScanType],
        extracted_mapped_annotation: Optional[_AnnotationScanType],
        is_dataclass_field: bool,
    ) -> None:
        column = self.columns[0]
        if column.key is None:
            column.key = key
        if column.name is None:
            column.name = key

    @property
    def mapper_property_to_assign(self) -> Optional["MapperProperty[_T]"]:
        return self

    @property
    def columns_to_assign(self) -> List[Column[Any]]:
        # mypy doesn't care about the isinstance here
        return [
            c  # type: ignore
            for c in self.columns
            if isinstance(c, Column) and c.table is None
        ]

    def _memoized_attr__renders_in_subqueries(self) -> bool:
        return ("deferred", True) not in self.strategy_key or (
            self not in self.parent._readonly_props  # type: ignore
        )

    @util.preload_module("sqlalchemy.orm.state", "sqlalchemy.orm.strategies")
    def _memoized_attr__deferred_column_loader(
        self,
    ) -> _InstallLoaderCallableProto[Any]:
        state = util.preloaded.orm_state
        strategies = util.preloaded.orm_strategies
        return state.InstanceState._instance_level_callable_processor(
            self.parent.class_manager,
            strategies.LoadDeferredColumns(self.key),
            self.key,
        )

    @util.preload_module("sqlalchemy.orm.state", "sqlalchemy.orm.strategies")
    def _memoized_attr__raise_column_loader(
        self,
    ) -> _InstallLoaderCallableProto[Any]:
        state = util.preloaded.orm_state
        strategies = util.preloaded.orm_strategies
        return state.InstanceState._instance_level_callable_processor(
            self.parent.class_manager,
            strategies.LoadDeferredColumns(self.key, True),
            self.key,
        )

    def __clause_element__(self) -> roles.ColumnsClauseRole:
        """Allow the ColumnProperty to work in expression before it is turned
        into an instrumented attribute.
        """

        return self.expression

    @property
    def expression(self) -> roles.ColumnsClauseRole:
        """Return the primary column or expression for this ColumnProperty.

        E.g.::


            class File(Base):
                # ...

                name = Column(String(64))
                extension = Column(String(8))
                filename = column_property(name + '.' + extension)
                path = column_property('C:/' + filename.expression)

        .. seealso::

            :ref:`mapper_column_property_sql_expressions_composed`

        """
        return self.columns[0]

    def instrument_class(self, mapper: Mapper[Any]) -> None:
        if not self.instrument:
            return

        attributes.register_descriptor(
            mapper.class_,
            self.key,
            comparator=self.comparator_factory(self, mapper),
            parententity=mapper,
            doc=self.doc,
        )

    def do_init(self) -> None:
        super().do_init()

        if len(self.columns) > 1 and set(self.parent.primary_key).issuperset(
            self.columns
        ):
            util.warn(
                (
                    "On mapper %s, primary key column '%s' is being combined "
                    "with distinct primary key column '%s' in attribute '%s'. "
                    "Use explicit properties to give each column its own "
                    "mapped attribute name."
                )
                % (self.parent, self.columns[1], self.columns[0], self.key)
            )

    def copy(self) -> ColumnProperty[_T]:
        return ColumnProperty(
            *self.columns,
            deferred=self.deferred,
            group=self.group,
            active_history=self.active_history,
        )

    def merge(
        self,
        session: Session,
        source_state: InstanceState[Any],
        source_dict: _InstanceDict,
        dest_state: InstanceState[Any],
        dest_dict: _InstanceDict,
        load: bool,
        _recursive: Dict[Any, object],
        _resolve_conflict_map: Dict[_IdentityKeyType[Any], object],
    ) -> None:
        if not self.instrument:
            return
        elif self.key in source_dict:
            value = source_dict[self.key]

            if not load:
                dest_dict[self.key] = value
            else:
                impl = dest_state.get_impl(self.key)
                impl.set(dest_state, dest_dict, value, None)
        elif dest_state.has_identity and self.key not in dest_dict:
            dest_state._expire_attributes(
                dest_dict, [self.key], no_loader=True
            )

    class Comparator(util.MemoizedSlots, PropComparator[_PT]):
        """Produce boolean, comparison, and other operators for
        :class:`.ColumnProperty` attributes.

        See the documentation for :class:`.PropComparator` for a brief
        overview.

        .. seealso::

            :class:`.PropComparator`

            :class:`.ColumnOperators`

            :ref:`types_operators`

            :attr:`.TypeEngine.comparator_factory`

        """

        if not TYPE_CHECKING:
            # prevent pylance from being clever about slots
            __slots__ = "__clause_element__", "info", "expressions"

        prop: RODescriptorReference[ColumnProperty[_PT]]

        def _orm_annotate_column(self, column: _NC) -> _NC:
            """annotate and possibly adapt a column to be returned
            as the mapped-attribute exposed version of the column.

            The column in this context needs to act as much like the
            column in an ORM mapped context as possible, so includes
            annotations to give hints to various ORM functions as to
            the source entity of this column.   It also adapts it
            to the mapper's with_polymorphic selectable if one is
            present.

            """

            pe = self._parententity
            annotations: Dict[str, Any] = {
                "entity_namespace": pe,
                "parententity": pe,
                "parentmapper": pe,
                "proxy_key": self.prop.key,
            }

            col = column

            # for a mapper with polymorphic_on and an adapter, return
            # the column against the polymorphic selectable.
            # see also orm.util._orm_downgrade_polymorphic_columns
            # for the reverse operation.
            if self._parentmapper._polymorphic_adapter:
                mapper_local_col = col
                col = self._parentmapper._polymorphic_adapter.traverse(col)

                # this is a clue to the ORM Query etc. that this column
                # was adapted to the mapper's polymorphic_adapter.  the
                # ORM uses this hint to know which column its adapting.
                annotations["adapt_column"] = mapper_local_col

            return col._annotate(annotations)._set_propagate_attrs(
                {"compile_state_plugin": "orm", "plugin_subject": pe}
            )

        if TYPE_CHECKING:

            def __clause_element__(self) -> NamedColumn[_PT]:
                ...

        def _memoized_method___clause_element__(
            self,
        ) -> NamedColumn[_PT]:
            if self.adapter:
                return self.adapter(self.prop.columns[0], self.prop.key)
            else:
                return self._orm_annotate_column(self.prop.columns[0])

        def _memoized_attr_info(self) -> _InfoType:
            """The .info dictionary for this attribute."""

            ce = self.__clause_element__()
            try:
                return ce.info  # type: ignore
            except AttributeError:
                return self.prop.info

        def _memoized_attr_expressions(self) -> Sequence[NamedColumn[Any]]:
            """The full sequence of columns referenced by this
            attribute, adjusted for any aliasing in progress.

            .. versionadded:: 1.3.17

            """
            if self.adapter:
                return [
                    self.adapter(col, self.prop.key)
                    for col in self.prop.columns
                ]
            else:
                return [
                    self._orm_annotate_column(col) for col in self.prop.columns
                ]

        def _fallback_getattr(self, key: str) -> Any:
            """proxy attribute access down to the mapped column.

            this allows user-defined comparison methods to be accessed.
            """
            return getattr(self.__clause_element__(), key)

        def operate(
            self, op: OperatorType, *other: Any, **kwargs: Any
        ) -> ColumnElement[Any]:
            return op(self.__clause_element__(), *other, **kwargs)  # type: ignore[return-value]  # noqa: E501

        def reverse_operate(
            self, op: OperatorType, other: Any, **kwargs: Any
        ) -> ColumnElement[Any]:
            col = self.__clause_element__()
            return op(col._bind_param(op, other), col, **kwargs)  # type: ignore[return-value]  # noqa: E501

    def __str__(self) -> str:
        if not self.parent or not self.key:
            return object.__repr__(self)
        return str(self.parent.class_.__name__) + "." + self.key


class MappedColumn(
    SQLCoreOperations[_T],
    _IntrospectsAnnotations,
    _MapsColumns[_T],
):
    """Maps a single :class:`_schema.Column` on a class.

    :class:`_orm.MappedColumn` is a specialization of the
    :class:`_orm.ColumnProperty` class and is oriented towards declarative
    configuration.

    To construct :class:`_orm.MappedColumn` objects, use the
    :func:`_orm.mapped_column` constructor function.

    .. versionadded:: 2.0


    """

    __slots__ = (
        "column",
        "_creation_order",
        "foreign_keys",
        "_has_nullable",
        "deferred",
        "_attribute_options",
        "_has_dataclass_arguments",
    )

    deferred: bool
    column: Column[_T]
    foreign_keys: Optional[Set[ForeignKey]]
    _attribute_options: _AttributeOptions

    def __init__(self, *arg: Any, **kw: Any):
        self._attribute_options = attr_opts = kw.pop(
            "attribute_options", _DEFAULT_ATTRIBUTE_OPTIONS
        )

        self._has_dataclass_arguments = False

        if attr_opts is not None and attr_opts != _DEFAULT_ATTRIBUTE_OPTIONS:
            if attr_opts.dataclasses_default_factory is not _NoArg.NO_ARG:
                self._has_dataclass_arguments = True
                kw["default"] = attr_opts.dataclasses_default_factory
            elif attr_opts.dataclasses_default is not _NoArg.NO_ARG:
                kw["default"] = attr_opts.dataclasses_default

            if (
                attr_opts.dataclasses_init is not _NoArg.NO_ARG
                or attr_opts.dataclasses_repr is not _NoArg.NO_ARG
            ):
                self._has_dataclass_arguments = True

        if "default" in kw and kw["default"] is _NoArg.NO_ARG:
            kw.pop("default")

        self.deferred = kw.pop("deferred", False)
        self.column = cast("Column[_T]", Column(*arg, **kw))
        self.foreign_keys = self.column.foreign_keys
        self._has_nullable = "nullable" in kw and kw.get("nullable") not in (
            None,
            SchemaConst.NULL_UNSPECIFIED,
        )
        util.set_creation_order(self)

    def _copy(self: Self, **kw: Any) -> Self:
        new = cast(Self, self.__class__.__new__(self.__class__))
        new.column = self.column._copy(**kw)
        new.deferred = self.deferred
        new.foreign_keys = new.column.foreign_keys
        new._has_nullable = self._has_nullable
        new._attribute_options = self._attribute_options
        new._has_dataclass_arguments = self._has_dataclass_arguments
        util.set_creation_order(new)
        return new

    @property
    def mapper_property_to_assign(self) -> Optional["MapperProperty[_T]"]:
        if self.deferred:
            return ColumnProperty(
                self.column,
                deferred=True,
                attribute_options=self._attribute_options,
            )
        else:
            return None

    @property
    def columns_to_assign(self) -> List[Column[Any]]:
        return [self.column]

    def __clause_element__(self) -> Column[_T]:
        return self.column

    def operate(
        self, op: OperatorType, *other: Any, **kwargs: Any
    ) -> ColumnElement[Any]:
        return op(self.__clause_element__(), *other, **kwargs)  # type: ignore[return-value]  # noqa: E501

    def reverse_operate(
        self, op: OperatorType, other: Any, **kwargs: Any
    ) -> ColumnElement[Any]:
        col = self.__clause_element__()
        return op(col._bind_param(op, other), col, **kwargs)  # type: ignore[return-value]  # noqa: E501

    def declarative_scan(
        self,
        registry: _RegistryType,
        cls: Type[Any],
        key: str,
        annotation: Optional[_AnnotationScanType],
        extracted_mapped_annotation: Optional[_AnnotationScanType],
        is_dataclass_field: bool,
    ) -> None:
        column = self.column
        if column.key is None:
            column.key = key
        if column.name is None:
            column.name = key

        sqltype = column.type

        if extracted_mapped_annotation is None:
            if sqltype._isnull and not self.column.foreign_keys:
                self._raise_for_required(key, cls)
            else:
                return

        self._init_column_for_annotation(
            cls, registry, extracted_mapped_annotation
        )

    @util.preload_module("sqlalchemy.orm.decl_base")
    def declarative_scan_for_composite(
        self,
        registry: _RegistryType,
        cls: Type[Any],
        key: str,
        param_name: str,
        param_annotation: _AnnotationScanType,
    ) -> None:
        decl_base = util.preloaded.orm_decl_base
        decl_base._undefer_column_name(param_name, self.column)
        self._init_column_for_annotation(cls, registry, param_annotation)

    def _init_column_for_annotation(
        self,
        cls: Type[Any],
        registry: _RegistryType,
        argument: _AnnotationScanType,
    ) -> None:
        sqltype = self.column.type

        nullable = False

        if hasattr(argument, "__origin__"):
            nullable = NoneType in argument.__args__  # type: ignore

        if not self._has_nullable:
            self.column.nullable = nullable

        if sqltype._isnull and not self.column.foreign_keys:
            new_sqltype = None
            our_type = de_optionalize_union_types(argument)

            if is_fwd_ref(our_type):
                our_type = de_stringify_annotation(cls, our_type)

            if registry.type_annotation_map:
                new_sqltype = registry.type_annotation_map.get(our_type)
            if new_sqltype is None:
                new_sqltype = sqltypes._type_map_get(our_type)  # type: ignore

            if new_sqltype is None:
                raise sa_exc.ArgumentError(
                    f"Could not locate SQLAlchemy Core "
                    f"type for Python type: {our_type}"
                )
            self.column.type = new_sqltype  # type: ignore
