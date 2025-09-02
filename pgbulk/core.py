import itertools
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    List,
    Literal,
    NamedTuple,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

from asgiref.sync import sync_to_async
from django import __version__ as DJANGO_VERSION
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, models
from django.db.models import expressions
from django.db.models.sql.compiler import SQLCompiler
from django.utils import timezone
from django.utils.version import get_version_tuple

if TYPE_CHECKING:
    from typing_extensions import Final, TypeAlias

UpdateFieldsTypeDef: "TypeAlias" = Union[
    List[str], List["UpdateField"], List[Union["UpdateField", str]], None
]
_M = TypeVar("_M", bound=models.Model)
QuerySet: "TypeAlias" = Union[Type[_M], models.QuerySet[_M]]
AnyField: "TypeAlias" = "models.Field[Any, Any]"
Expression: "TypeAlias" = "models.Expression | models.F"

_PRECISION_SPECIFIER_RE: "Final" = re.compile(r"\(.*?\)")


class _DB_DEFAULT:
    """Sentinel value for a database default."""


if TYPE_CHECKING:
    from django.db import DefaultConnectionProxy
    from django.db.backends.utils import CursorWrapper

    class Row(NamedTuple):
        """Represents a row returned by an upsert operation."""

        status_: Literal["u", "c"]

        def __getattr__(self, item: str) -> Any: ...


def _psycopg_version() -> Tuple[int, int, int]:
    try:
        import psycopg as Database  # type: ignore
    except ImportError:
        import psycopg2 as Database
    except Exception as exc:  # pragma: no cover
        raise ImproperlyConfigured("Error loading psycopg2 or psycopg module") from exc

    version_tuple = get_version_tuple(Database.__version__.split(" ", 1)[0])  # type: ignore

    if version_tuple[0] not in (2, 3):  # pragma: no cover
        raise ImproperlyConfigured(f"Pysocpg version {version_tuple[0]} not supported")

    return version_tuple


django_version = DJANGO_VERSION
psycopg_version = _psycopg_version()
psycopg_maj_version = psycopg_version[0]


if psycopg_maj_version == 2:
    from psycopg2.extensions import quote_ident  # type: ignore
elif psycopg_maj_version == 3:
    import psycopg.adapt  # type: ignore
    from psycopg.pq import Escaping  # type: ignore

    class LiteralValue:  # pragma: no cover
        def __init__(self, val: str) -> None:
            self.val = val

    class LiteralDumper(psycopg.adapt.Dumper):  # pragma: no cover # type: ignore
        def dump(self, obj: Any) -> bytes:
            return obj.val.encode("utf-8")

        def quote(self, obj: Any) -> bytes:
            return self.dump(obj)

else:
    raise AssertionError


class UpdateField(str):
    """
    For expressing an update field as an expression to an upsert
    operation.

    Example:

        results = pgbulk.upsert(
            MyModel,
            [
                MyModel(some_int_field=0, some_key="a"),
                MyModel(some_int_field=0, some_key="b")
            ],
            ["some_key"],
            [
                pgbulk.UpdateField(
                    "some_int_field",
                    expression=models.F('some_int_field') + 1
                )
            ],
        )
    """

    expression: Union[Expression, None]

    def __new__(cls, field: str, expression: Union[Expression, None] = None) -> "UpdateField":
        obj = super().__new__(cls, field)
        obj.expression = expression
        return obj


class UpsertResult(List["Row"]):
    """
    Returned by [pgbulk.upsert][] when the `returning` argument is provided.

    Wraps a list of named tuples where the names correspond to the underlying
    Django model attribute names.

    Also provides properties to access created and updated rows.
    """

    @property
    def created(self) -> List["Row"]:
        """Return the created rows"""
        return [i for i in self if i.status_ == "c"]

    @property
    def updated(self) -> List["Row"]:
        """Return the updated rows"""
        return [i for i in self if i.status_ == "u"]


def _quote(field: str, cursor: "CursorWrapper") -> str:
    """Quote identifiers."""
    if psycopg_maj_version == 2:
        return quote_ident(field, cursor.connection)  # type: ignore
    else:
        return (
            Escaping(cursor.connection.pgconn)  # type: ignore
            .escape_identifier(field.encode())
            .decode()
        )


def _filter_fields(
    queryset: models.QuerySet[_M],
    fields: UpdateFieldsTypeDef,
    exclude: Union[List[str], None] = None,
    exclude_non_updatable: bool = True,
) -> List[Union[str, UpdateField]]:
    """
    Filter fields.

    Always exclude non-concrete fields and auto fields.
    Optionally exclude auto_now_add and primary keys.
    """
    exclude = exclude or []
    model = queryset.model
    model_fields = _model_fields(model)
    all_fields = {
        **{field.attname: field for field in model_fields},
        **{field.name: field for field in model_fields},
    }

    if fields is None:
        fields = [field.attname for field in model_fields]

    to_update = [
        attname
        for attname in fields
        if (
            attname not in exclude
            and not isinstance(all_fields[attname], models.AutoField)
            and (
                not exclude_non_updatable
                or (
                    not getattr(all_fields[attname], "auto_now_add", False)
                    and not all_fields[attname].primary_key
                )
            )
        )
    ]

    return to_update


def _fill_auto_fields(queryset: models.QuerySet[_M], values: Iterable[_M]) -> Iterable[_M]:
    """
    Given a list of models, fill in auto_now and auto_now_add fields
    for upserts. Since django manager utils passes Django's ORM, these values
    have to be automatically constructed
    """
    model = queryset.model
    auto_field_names = [
        f.attname
        for f in _model_fields(model)
        if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)
    ]
    now = timezone.now()
    for value in values:
        for f in auto_field_names:
            setattr(value, f, now)

    return values


def _prep_sql_args(
    queryset: models.QuerySet[_M],
    cursor: "CursorWrapper",
    sql_args: List[Any],
) -> List[Any]:
    if psycopg_maj_version == 3:
        cursor.adapters.register_dumper(LiteralValue, LiteralDumper)  # type: ignore

    compiler = SQLCompiler(
        query=queryset.query,
        connection=cursor.connection,
        using=queryset.using,  # type: ignore
    )

    return [
        LiteralValue(cursor.mogrify(*sql_arg.as_sql(compiler, cursor.connection)).decode("utf-8"))
        if hasattr(sql_arg, "as_sql")
        else sql_arg
        for sql_arg in sql_args
    ]


def _value_is_db_default(value: Any) -> bool:
    """Check if the value is a database default, they were introduced in Django 5.0"""
    return django_version >= "5.0" and isinstance(value, expressions.DatabaseDefault)  # type: ignore - missing stubs


def _get_field_db_val(
    queryset: models.QuerySet[_M],
    field: AnyField,
    value: Any,
    connection: "DefaultConnectionProxy",
    *,
    copying: bool = False,
) -> "Any | type[_DB_DEFAULT]":
    if _value_is_db_default(value):
        if not copying:
            return _DB_DEFAULT
        else:
            raise ValueError(
                (
                    "DB defaults are not supported when copying, "
                    "please provide an ORM default with `default=`"
                )
            )

    elif hasattr(value, "resolve_expression"):  # pragma: no cover
        # Handle cases when the field is of type "Func" and other expressions.
        # This is useful for libraries like django-rdkit that can't easily be tested
        return value.resolve_expression(queryset.query, allow_joins=False, for_save=True)
    else:
        return field.get_db_prep_save(value, connection)


def _sort_by_unique_fields(
    queryset: models.QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
) -> List[_M]:
    """
    Sort a list of models by their unique fields.

    Sorting models in an upsert greatly reduces the chances of deadlock
    when doing concurrent upserts
    """
    model = queryset.model
    connection = connections[queryset.db]
    unique_db_fields = [field for field in _model_fields(model) if field.attname in unique_fields]

    def sort_key(model_obj: _M) -> Tuple[Any, ...]:
        return tuple(
            _get_field_db_val(queryset, field, getattr(model_obj, field.attname), connection)
            for field in unique_db_fields
        )

    return sorted(model_objs, key=sort_key)


def _get_values_for_row(
    queryset: models.QuerySet[_M],
    model_obj: _M,
    all_fields: List[AnyField],
    *,
    copying: bool = False,
) -> List[Any]:
    connection = connections[queryset.db]
    return [
        # Convert field value to db value
        # Use attname here to support fields with custom db_column names
        _get_field_db_val(
            queryset, field, getattr(model_obj, field.attname), connection, copying=copying
        )
        for field in all_fields
    ]


def _format_placeholders_row(
    values_for_row: List[Any],
    all_fields: List[AnyField],
    connection: "DefaultConnectionProxy",
    *,
    include_cast: bool,
) -> str:
    placeholders = ", ".join(
        f"{'%s'}{f'::{field.db_type(connection)}' if include_cast else ''}"
        if val is not _DB_DEFAULT
        else "DEFAULT"
        for val, field in zip(values_for_row, all_fields)
    )
    return f"({placeholders})"


def _get_values_for_rows(
    queryset: models.QuerySet[_M],
    model_objs: Iterable[_M],
    all_fields: List[AnyField],
) -> Tuple[List[str], List[Any]]:
    connection = connections[queryset.db]
    row_values: List[str] = []
    sql_args: List[Any] = []

    for i, model_obj in enumerate(model_objs):
        values_for_row = _get_values_for_row(queryset, model_obj, all_fields)
        sql_args.extend((val for val in values_for_row if val is not _DB_DEFAULT))
        if i == 0:
            row_values.append(
                _format_placeholders_row(values_for_row, all_fields, connection, include_cast=True)
            )
        else:
            row_values.append(
                _format_placeholders_row(
                    values_for_row, all_fields, connection, include_cast=False
                )
            )

    return row_values, sql_args


def _get_returning_sql(
    returning: Union[List[str], bool],
    model: Type[models.Model],
    cursor: "CursorWrapper",
    include_status: bool,
) -> str:
    returning = returning if returning is not True else [f.column for f in _model_fields(model)]
    if not returning:
        return ""

    table = _quote(model._meta.db_table, cursor)
    returning_sql = ", ".join(f"{table}.{_quote(field, cursor)}" for field in returning)
    if include_status:
        returning_sql += ", CASE WHEN xmax = 0 THEN 'c' ELSE 'u' END AS status_"

    return "RETURNING " + returning_sql


def _model_fields(model: Type[models.Model]) -> List["models.Field[Any, Any]"]:
    """Return the fields of a model, excluding generated and non-concrete ones."""
    return [f for f in model._meta.fields if not getattr(f, "generated", False) and f.concrete]


def _get_update_fields_sql(
    queryset: models.QuerySet[_M],
    fields: List[Union[str, UpdateField]],
    alias: str,
    ignore_unchanged: bool,
    cursor: "CursorWrapper",
) -> Tuple[str, str]:
    """Render the SET and WHERE clause of update for every update field.

    If the WHERE clause is returned, it means we're ignoring unchanged rows.
    """
    connection = connections[queryset.db]
    model = queryset.model
    cols = [model._meta.get_field(update_field).column for update_field in fields]
    update_expressions = {
        f: f.expression for f in fields if isinstance(f, UpdateField) and f.expression
    }
    update_fields_expressions = {col: f"{alias}.{_quote(col, cursor)}" for col in cols}
    if update_expressions:
        connection = connections[queryset.db]
        compiler = SQLCompiler(query=queryset.query, connection=connection, using=queryset.using)  # type: ignore
        with connection.cursor() as cursor:
            for field_name, expr in update_expressions.items():
                expr = expr.resolve_expression(queryset.query, allow_joins=False, for_save=True)
                val = cursor.mogrify(*expr.as_sql(compiler, connection))  #  type: ignore
                val = cast(Union[str, bytes], val)
                if isinstance(val, bytes):  # Psycopg 2/3 return different types
                    val = val.decode("utf-8")
                update_fields_expressions[model._meta.get_field(field_name).column] = val

    set_sql = ", ".join(
        f"{_quote(col, cursor)} = {update_fields_expressions[col]}" for col in cols
    )
    ignore_unchanged_sql = ""
    if ignore_unchanged and cols:
        ignore_unchanged_sql = ("(({fields_sql}) IS DISTINCT FROM ({expressions_sql}))").format(
            fields_sql=", ".join(
                "{0}.{1}".format(_quote(model._meta.db_table, cursor), _quote(col, cursor))
                for col in cols
            ),
            expressions_sql=", ".join(update_fields_expressions.values()),
        )

    return set_sql, ignore_unchanged_sql


def _get_upsert_sql(
    queryset: models.QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: List[Union[str, UpdateField]],
    returning: Union[List[str], bool],
    ignore_unchanged: bool,
    cursor: "CursorWrapper",
) -> Tuple[str, List[Any]]:
    """
    Generates the postgres specific sql necessary to perform an upsert
    (ON CONFLICT) INSERT INTO table_name (field1, field2)
    VALUES (1, 'two')
    ON CONFLICT (unique_field) DO UPDATE SET field2 = EXCLUDED.field2;
    """
    model = queryset.model
    # Use all fields except auto fields unless in the uniqueness constraint
    all_fields = [
        field
        for field in _model_fields(model)
        if field.column in unique_fields or not isinstance(field, models.AutoField)
    ]

    all_field_names = [field.column for field in all_fields]
    all_field_names_sql = ", ".join([_quote(field, cursor) for field in all_field_names])

    # Convert field names to db column names
    unique_db_cols = [model._meta.get_field(unique_field).column for unique_field in unique_fields]
    update_db_cols = [model._meta.get_field(update_field).column for update_field in update_fields]

    row_values, sql_args = _get_values_for_rows(queryset, model_objs, all_fields)
    unique_field_names_sql = ", ".join([_quote(col, cursor) for col in unique_db_cols])
    update_fields_sql, ignore_unchanged_sql = _get_update_fields_sql(
        queryset=queryset,
        fields=update_fields,
        alias="EXCLUDED",
        ignore_unchanged=ignore_unchanged,
        cursor=cursor,
    )
    if ignore_unchanged_sql:
        ignore_unchanged_sql = f"WHERE {ignore_unchanged_sql}"

    return_sql = _get_returning_sql(returning, model=model, cursor=cursor, include_status=True)

    on_conflict = (
        "DO UPDATE SET {0} {1}".format(update_fields_sql, ignore_unchanged_sql)
        if update_db_cols
        else "DO NOTHING"
    )

    row_values_sql = ", ".join(row_values)
    sql = (
        " INSERT INTO {table_name} ({all_field_names_sql})"
        " VALUES {row_values_sql}"
        " ON CONFLICT ({unique_field_names_sql}) {on_conflict} {return_sql}"
    ).format(
        table_name=model._meta.db_table,
        all_field_names_sql=all_field_names_sql,
        row_values_sql=row_values_sql,
        unique_field_names_sql=unique_field_names_sql,
        on_conflict=on_conflict,
        return_sql=return_sql,
    )

    return sql, sql_args


def _upsert(
    queryset: models.QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef,
    exclude: Union[List[str], None],
    returning: Union[List[str], bool],
    ignore_unchanged: bool,
    cursor: "CursorWrapper",
) -> Union[UpsertResult, None]:
    """Internal implementation of bulk upsert."""
    exclude = exclude or []

    # Populate automatically generated fields in the rows like date times
    _fill_auto_fields(queryset, model_objs)

    # Sort the rows to reduce the chances of deadlock during concurrent upserts
    model_objs = _sort_by_unique_fields(queryset, model_objs, unique_fields)
    update_fields = _filter_fields(queryset, update_fields, exclude=[*exclude, *unique_fields])  # type: ignore

    upserted: List["Row"] = []

    if model_objs:
        sql, sql_args = _get_upsert_sql(
            queryset,
            model_objs,
            unique_fields=unique_fields,
            update_fields=update_fields,
            returning=returning,
            ignore_unchanged=ignore_unchanged,
            cursor=cursor,
        )

        sql_args = _prep_sql_args(queryset, cursor=cursor, sql_args=sql_args)
        cursor.execute(sql, sql_args)
        if cursor.description:
            result = [(col.name, Any) for col in cursor.description]
            nt_result = NamedTuple("Result", result)
            upserted = cast(List["Row"], [nt_result(*row) for row in cursor.fetchall()])

    return UpsertResult(upserted) if returning else None


def _update(
    queryset: models.QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None],
    exclude: Union[List[str], None],
    returning: Union[List[str], bool],
    ignore_unchanged: bool,
    cursor: "CursorWrapper",
) -> Union[List["Row"], None]:
    """
    Core update implementation
    """
    model = queryset.model
    connection = connections[queryset.db]
    alias = "new_values"
    update_fields = _filter_fields(queryset, update_fields, exclude)  # type: ignore
    update_db_cols = [model._meta.get_field(update_field).column for update_field in update_fields]

    # Sort the model objects to reduce the likelihood of deadlocks
    model_objs = sorted(model_objs, key=lambda obj: obj.pk)

    if not model._meta.pk:  # pragma: no cover - for type-safety
        raise ValueError("Model must have a primary key to perform a bulk update.")

    # Add the pk to the value fields so we can join during the update.
    value_fields = [model._meta.pk.attname] + update_fields

    row_values = [
        [
            _get_field_db_val(
                queryset,
                model_obj._meta.get_field(field),
                getattr(model_obj, model_obj._meta.get_field(field).attname),
                connection,
            )
            for field in value_fields
        ]
        for model_obj in model_objs
    ]

    # If we do not have any values or fields to update, just return
    if len(row_values) == 0 or len(update_fields) == 0:
        return [] if returning else None

    db_types = [model._meta.get_field(field).db_type(connection) for field in value_fields]

    value_fields_sql = ", ".join(
        "{field}".format(field=_quote(model._meta.get_field(field).column, cursor))
        for field in value_fields
    )

    update_fields_sql = ", ".join(
        "{field} = {alias}.{field}".format(field=_quote(col, cursor), alias=alias)
        for col in update_db_cols
    )
    update_fields_sql, ignore_unchanged_sql = _get_update_fields_sql(
        queryset=queryset,
        fields=update_fields,
        alias=alias,
        ignore_unchanged=ignore_unchanged,
        cursor=cursor,
    )

    values_sql = ", ".join(
        [
            "({0})".format(
                ", ".join(
                    [
                        "%s::{0}".format(db_types[i]) if not row_number and i else "%s"
                        for i, _ in enumerate(row)
                    ]
                )
            )
            for row_number, row in enumerate(row_values)
        ]
    )

    if ignore_unchanged_sql:
        ignore_unchanged_sql = f"AND {ignore_unchanged_sql}"

    update_sql = (
        "UPDATE {table} "
        "SET {update_fields_sql} "
        "FROM (VALUES {values_sql}) AS {alias} ({value_fields_sql}) "
        "WHERE {table}.{pk_field} = new_values.{pk_field} {ignore_unchanged_sql} "
        "{returning_sql}"
    ).format(
        table=_quote(model._meta.db_table, cursor),
        pk_field=_quote(model._meta.pk.column, cursor),
        alias=alias,
        update_fields_sql=update_fields_sql,
        values_sql=values_sql,
        value_fields_sql=value_fields_sql,
        ignore_unchanged_sql=ignore_unchanged_sql,
        returning_sql=_get_returning_sql(
            returning=returning, model=model, include_status=False, cursor=cursor
        ),
    )

    update_sql_params = list(itertools.chain(*row_values))
    update_sql_params = _prep_sql_args(queryset, cursor=cursor, sql_args=update_sql_params)
    cursor.execute(update_sql, update_sql_params)
    updated: List["Row"] = []
    if cursor.description:
        result = [(col.name, Any) for col in cursor.description]
        nt_result = NamedTuple("Result", result)
        updated = cast(List["Row"], [nt_result(*row) for row in cursor.fetchall()])

    return updated if returning else None


@overload
def update(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], Literal[True]],
    ignore_unchanged: bool = False,
) -> List["Row"]: ...


@overload
def update(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Literal[False] = False,
    ignore_unchanged: bool = False,
) -> None: ...


@overload
def update(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[List["Row"], None]: ...


def update(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[List["Row"], None]:
    """
    Performs a bulk update.

    Args:
        queryset: A model or a queryset for the table being updated.
        model_objs: Model object values to use for the update.
        update_fields: A list of fields on the model objects to update.
            If `None`, all fields will be updated.
        exclude: A list of fields to exclude from the update. This is useful
            when `update_fields` is `None` and you want to exclude fields from
            being updated.
        returning: If True, returns all fields. If a list, only returns fields
            in the list. If False, do not return results from the upsert.
        ignore_unchanged: Ignore unchanged rows in updates.

    Note:
        Model signals such as `post_save` are not emitted.

    Returns:
        If `returning=True`, an iterable list of all updated objects.
    """
    queryset = queryset if isinstance(queryset, models.QuerySet) else queryset.objects.all()
    with connections[queryset.db].cursor() as cursor:
        return _update(
            queryset=queryset,
            model_objs=model_objs,
            update_fields=update_fields,
            exclude=exclude,
            returning=returning,
            ignore_unchanged=ignore_unchanged,
            cursor=cursor,
        )


@overload
async def aupdate(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], Literal[True]],
    ignore_unchanged: bool = False,
) -> List["Row"]: ...


@overload
async def aupdate(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Literal[False] = False,
    ignore_unchanged: bool = False,
) -> None: ...


@overload
async def aupdate(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[List["Row"], None]: ...


async def aupdate(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    update_fields: Union[List[str], None] = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[List["Row"], None]:
    """
    Perform an asynchronous bulk update.

    See [pgbulk.update][]

    Note:
        Like other async Django ORM methods, `aupdate` currently wraps `update` in
        a `sync_to_async` wrapper. It does not yet use an asynchronous database
        driver but will in the future.
    """
    return await sync_to_async(update)(
        queryset,
        model_objs,
        update_fields=update_fields,
        exclude=exclude,
        returning=returning,
        ignore_unchanged=ignore_unchanged,
    )


@overload
def upsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], Literal[True]],
    ignore_unchanged: bool = False,
) -> UpsertResult: ...


@overload
def upsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Literal[False] = False,
    ignore_unchanged: bool = False,
) -> None: ...


@overload
def upsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[UpsertResult, None]: ...


def upsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[UpsertResult, None]:
    """
    Perform a bulk upsert.

    Args:
        queryset: A model or a queryset for the table being upserted.
        model_objs: An iterable of Django models to upsert. All models
            in this list will be bulk upserted.
        unique_fields: A list of fields that define the uniqueness
            of the model. The model must have a unique constraint on these
            fields.
        update_fields: A list of fields to update whenever objects already exist.
            If an empty list is provided, it is equivalent to doing a bulk insert on
            the objects that don't exist. If `None`, all fields will be updated.
            If you want to perform an expression such as an `F` object on a field when
            it is updated, use the [pgbulk.UpdateField][] class. See examples below.
        exclude: A list of fields to exclude from the upsert. This is useful
            when `update_fields` is `None` and you want to exclude fields from
            being updated. This is additive to the `unique_fields` list.
        returning: If True, returns all fields. If a list, only returns fields
            in the list. If False, do not return results from the upsert.
        ignore_unchanged: Ignore unchanged rows in updates.

    Returns:
        If `returning=True`, the upserted result, an iterable list of all upsert objects.
            Use the `.updated` and `.created` attributes to iterate over created or updated
            elements.

    Note:
        Model signals such as `post_save` are not emitted.
    """
    queryset = queryset if isinstance(queryset, models.QuerySet) else queryset.objects.all()
    with connections[queryset.db].cursor() as cursor:
        return _upsert(
            queryset,
            model_objs,
            unique_fields=unique_fields,
            update_fields=update_fields,
            returning=returning,
            exclude=exclude,
            ignore_unchanged=ignore_unchanged,
            cursor=cursor,
        )


@overload
async def aupsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], Literal[True]],
    ignore_unchanged: bool = False,
) -> UpsertResult: ...


@overload
async def aupsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Literal[False] = False,
    ignore_unchanged: bool = False,
) -> None: ...


@overload
async def aupsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[UpsertResult, None]: ...


async def aupsert(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    unique_fields: List[str],
    update_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    returning: Union[List[str], bool] = False,
    ignore_unchanged: bool = False,
) -> Union[UpsertResult, None]:
    """
    Perform an asynchronous bulk upsert.

    See [pgbulk.upsert][]

    Note:
        Like other async Django ORM methods, `aupsert` currently wraps `upsert` in
        a `sync_to_async` wrapper. It does not yet use an asynchronous database
        driver but will in the future.
    """
    return await sync_to_async(upsert)(
        queryset,
        model_objs,
        unique_fields=unique_fields,
        update_fields=update_fields,
        returning=returning,
        exclude=exclude,
        ignore_unchanged=ignore_unchanged,
    )


def _postgres_types_for_fields(
    fields: List["models.Field[Any, Any]"],
    connection: "DefaultConnectionProxy",
) -> List[str]:
    def _simplify_type(field_type: str) -> str:
        # Remove any size/precision/scale information, as Psycopg doesn't accept it.
        return _PRECISION_SPECIFIER_RE.sub("", field_type)

    return [_simplify_type(field.db_type(connection=connection)) for field in fields]


def copy(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    copy_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    binary: bool = False,
) -> None:
    """
    Copy data into a table.

    Args:
        queryset: queryset: A model or a queryset for the table being copied into.
        model_objs: An iterable of Django models to copy.
        copy_fields: A list of fields on the model objects to copy.
            If `None`, all fields will be copied.
        exclude: A list of fields to exclude from the copy. This is useful
            when `copy_fields` is `None` and you want to exclude fields from
            being copied.
        binary: If True, copy data in binary format.
            This can yield improved performance for large datasets.

    Note:
        Model signals such as `post_save` are not emitted.
    """
    if psycopg_maj_version == 2:  # pragma: no cover
        raise RuntimeError("Only psycopg3 is supported for pgbulk.copy.")

    queryset = queryset if isinstance(queryset, models.QuerySet) else queryset.objects.all()
    model = queryset.model

    # Populate automatically-generated fields in the rows like date times
    _fill_auto_fields(queryset, model_objs)

    # Determine which fields should be copied
    fields = [
        model._meta.get_field(field)
        for field in _filter_fields(
            queryset, copy_fields, exclude=exclude, exclude_non_updatable=False
        )
    ]
    cols = [field.column for field in fields]

    connection = connections[queryset.db]
    with connection.cursor() as cursor:
        all_field_names_sql = ", ".join([_quote(col, cursor) for col in cols])
        copy_sql = (
            f"COPY {_quote(model._meta.db_table, cursor)} ({all_field_names_sql}) FROM STDIN"
        )
        if binary:
            copy_sql += " WITH (FORMAT BINARY)"

        with cursor.copy(copy_sql) as copier:  # type: ignore
            if binary:
                postgres_types = _postgres_types_for_fields(fields, connection)
                copier.set_types(postgres_types)  # type: ignore

            for model_obj in model_objs:
                row = _get_values_for_row(queryset, model_obj, fields, copying=True)
                copier.write_row(row)  # type: ignore


async def acopy(
    queryset: QuerySet[_M],
    model_objs: Iterable[_M],
    copy_fields: UpdateFieldsTypeDef = None,
    *,
    exclude: Union[List[str], None] = None,
    binary: bool = False,
) -> None:
    """
    Asynchronously copy data into a table.

    See [pgbulk.copy][]

    Note:
        Like other async Django ORM methods, `acopy` currently wraps `copy` in
        a `sync_to_async` wrapper. It does not yet use an asynchronous database
        driver but will in the future.
    """
    return await sync_to_async(copy)(
        queryset,
        model_objs,
        copy_fields=copy_fields,
        exclude=exclude,
        binary=binary,
    )
