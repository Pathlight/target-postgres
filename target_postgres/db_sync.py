import sys
import json
import psycopg2
import psycopg2.extras
import singer
import collections
import inflection
import re
import itertools

logger = singer.get_logger()


JSONSCHEMA_TYPES = {
    'string',
    'number',
    'integer',
    'object',
    'array',
    'boolean',
    'null',
}


JSONSCHEMA_TYPE_TO_POSTGRES_TYPE = {
    'string': 'character varying',
    'number': 'numeric',
    'integer': 'bigint',
    'object': 'jsonb',
    'array': 'jsonb',
    'boolean': 'boolean',
}


_JSONSCHEMA_TYPE_CAN_CAST_TO = {
    'string': JSONSCHEMA_TYPES,
    'number': ('integer', 'boolean'),
    'integer': ('boolean',),
    'object': ('array',),
    'array': (),
    'boolean': (),
    # We never want to cast a non-null to null
    'null': ()
}


def get_usable_types(types):
    # Null is not usable as a discrete type (all types are nullable)
    types = set(types) - {'null',}
    # Return a new set that excludes any entry not in JSONSCHEMA_TYPES.
    return JSONSCHEMA_TYPES.intersection(types)


def get_castable_types(target_type):
    """
    Returns the set of types that can be safely converted to target_type
    """
    accepts = set(_JSONSCHEMA_TYPE_CAN_CAST_TO.get(target_type, ()))
    accepts |= {target_type,}
    return get_usable_types(accepts)


def most_general_type(types):
    """
    Figures out the most general type in the list, which allows us to make a
    more intelligent choice about which postgres data type to use.

    A type G is generalizes to a type T iff `cast(t::T AS G)` losslessly
    converts between the types without error. First we find the type that
    generalizes to the most other types in `types`. If that type can generalize
    to every type in types, we return it. If it can't, then we return 'string',
    as it is the most general type.
    """
    if not types:
        return 'string'

    types = get_usable_types(types)

    best_score, best_type = 0, None

    # Iterate over sorted types so that the same type always wins if two types
    # have equal scores.
    for t in sorted(types):
        castable_types = get_castable_types(t)
        # The score is the number of types in `types` that can be cast to the
        # type `t`. The most general one is the one that accepts the most casts
        # to it.
        score = len(castable_types.intersection(types))
        if score > best_score:
            best_score, best_type = score, t

    if best_type is None or not get_castable_types(best_type).issuperset(types):
        # bet_type is either None or can't accomodate all `types`, so return
        # `string`, which is the most general type of all.
        best_type = 'string'

    return best_type


def _column_type_generic(schema_property):
    property_format = schema_property['format'] if 'format' in schema_property else None
    types = schema_property['type']
    if isinstance(types, (str, bytes)):
        types = [types]
    concrete_type = most_general_type(types)

    if concrete_type == 'string':
        # jsonschema doesn't have a type for dates, so we need to go by the format
        if property_format == 'date-time':
            return 'timestamp with time zone'
        elif property_format == 'date':
            return 'date'

    if concrete_type not in JSONSCHEMA_TYPE_TO_POSTGRES_TYPE:
        concrete_type = 'string'
    return JSONSCHEMA_TYPE_TO_POSTGRES_TYPE[concrete_type]

def _column_type_simple(schema_property):
    property_type = schema_property['type']
    property_format = schema_property['format'] if 'format' in schema_property else None
    if 'object' in property_type or 'array' in property_type:
        return 'jsonb'
    elif property_format == 'date-time':
        return 'timestamp with time zone'
    elif property_format == 'date':
        return 'date'
    elif 'number' in property_type:
        return 'numeric'
    elif 'integer' in property_type and 'string' in property_type:
        return 'character varying'
    elif 'boolean' in property_type:
        return 'boolean'
    elif 'integer' in property_type:
        return 'bigint'
    else:
        return 'character varying'

def column_type(schema_property, use_simple: bool = True):
    if use_simple:
        return _column_type_simple(schema_property)
    return _column_type_generic(schema_property)


def inflect_name(name):

    # By modifying the name here , we introduce the possibility of having
    # duplicate column or table names. This will be much more likely to happen
    # with arbitrary user input
    name = re.sub(r'[^a-zA-Z0-9]', '_', name)
    name = re.sub(r"([A-Z]+)_([A-Z][a-z])", r'\1__\2', name)
    name = re.sub(r"([a-z\d])_([A-Z])", r'\1__\2', name)
    if name[0].isdigit():
        name = '_' + name
    return inflection.underscore(name)


def safe_column_name(name):
    return '"{}"'.format(name)


def column_clause(name, schema_property, use_simple_column_type: bool):
    return '{} {}'.format(
        safe_column_name(name),
        column_type(schema_property, use_simple_column_type),
    )


def flatten_key(k, parent_key, sep):
    full_key = parent_key + [k]
    inflected_key = [inflect_name(n) for n in full_key]
    reducer_index = 0
    while len(sep.join(inflected_key)) >= 63 and reducer_index < len(inflected_key):
        reduced_key = re.sub(r'[a-z]', '', inflection.camelize(inflected_key[reducer_index]))
        inflected_key[reducer_index] = \
            (reduced_key if len(reduced_key) > 1 else inflected_key[reducer_index][0:3]).lower()
        reducer_index += 1

    return sep.join(inflected_key)


def flatten_schema(d, parent_key=[], sep='__'):
    items = []
    for k, v in d['properties'].items():
        new_key = flatten_key(k, parent_key, sep)

        if 'anyOf' in v:
            # Added because tap-s3-csv uses `anyOf`. This will work for that
            # tap, but it may not work for all usages of `anyOf`.
            v = v['anyOf'][0]

        if 'type' in v.keys():
            if 'object' in v['type'] and 'properties' in v:
                items.extend(flatten_schema(v, parent_key + [k], sep=sep).items())
            else:
                items.append((new_key, v))
        else:
            if not v:
                continue
            if list(v.values())[0][0]['type'] == 'string':
                list(v.values())[0][0]['type'] = ['null', 'string']
                items.append((new_key, list(v.values())[0][0]))
            elif list(v.values())[0][0]['type'] == 'array':
                list(v.values())[0][0]['type'] = ['null', 'array']
                items.append((new_key, list(v.values())[0][0]))
            else:
                logger.warning('unhandled schema key: %s: %s', k, v)

    key_func = lambda item: item[0]
    sorted_items = sorted(items, key=key_func)
    for k, g in itertools.groupby(sorted_items, key=key_func):
        if len(list(g)) > 1:
            raise ValueError('Duplicate column name produced in schema: {}'.format(k))

    return dict(sorted_items)


def flatten_record(d, parent_key=[], sep='__'):
    if not d or not isinstance(d, dict):
        return {}
    items = []

    for k, v in d.items():
        new_key = flatten_key(k, parent_key, sep)
        if sys.version_info.minor >= 10:
            from collections.abc import MutableMapping
        else:
            from collections import MutableMapping # pylint: disable=E0611
        if isinstance(v, MutableMapping):
            items.extend(flatten_record(v, parent_key + [k], sep=sep).items())
            #  Note: without the following line, all the keys will be flattened
            #  including nested dictionaries.
            #  For example:
            #      {'answers': {'q1': 'some_string', 'some_dict': {'q2': 'some_string2'}}}
            #  will be transformed to
            #      [('answers__q1', 'some_string'), ('answers__some_dict__q2', 'some_string2')]
            #  so we need to add an additional item to be able to select the 'answers__some_dict' key.
            items.append((new_key, json.dumps(v)))
        else:
            items.append((new_key, json.dumps(v) if type(v) is list else v))
    return dict(items)


def primary_column_names(stream_schema_message):
    return [safe_column_name(inflect_name(p)) for p in stream_schema_message['key_properties']]


class DbSync:
    def __init__(self, target_config, stream_schema_message):
        self.target_config = target_config
        self.use_simple_column_type = self.target_config.get('use_simple_column_type', True)
        self.schema_name = self.target_config['schema']
        self.stream_schema_message = stream_schema_message
        self.flatten_schema = flatten_schema(stream_schema_message['schema'])

    def open_connection(self):
        conn_string = "host='{}' dbname='{}' user='{}' password='{}' port='{}'".format(
            self.target_config['host'],
            self.target_config['dbname'],
            self.target_config['user'],
            self.target_config['password'],
            self.target_config['port']
        )

        return psycopg2.connect(conn_string)

    def query(self, query, params=None):
        with self.open_connection() as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    query,
                    params
                )

                if cur.rowcount > 0:
                    return cur.fetchall()
                else:
                    return []

    def copy_from(self, file, table):
        with self.open_connection() as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.copy_from(file, table)

    def table_name(self, table_name, is_temporary):
        table_name = inflect_name(table_name)

        if is_temporary:
            return '{}_temp'.format(table_name)
        else:
            return '{}.{}'.format(self.schema_name, table_name)

    def record_primary_key_string(self, record):
        if len(self.stream_schema_message['key_properties']) == 0:
            return None
        if not record:
            return None
        flatten = flatten_record(record)
        key_props = [str(flatten[inflect_name(p)]) for p in self.stream_schema_message['key_properties']]
        return ','.join(key_props)

    def record_to_csv_row(self, record):
        row = []
        flatten = flatten_record(record)
        for name, schema in self.flatten_schema.items():
            if flatten.get(name) is not None:
                type = column_type(schema, use_simple=self.use_simple_column_type).lower()
                value = flatten[name]
                if type == 'jsonb':
                    try:
                        # Check to see if the value is a serialized JSON value.
                        json.loads(value)
                    except (TypeError, json.JSONDecodeError):
                        # value is not a valid JSON string, so make it one.
                        value = json.dumps(value)
            else:
                value = ''
            row.append(value)
        return row

    def load_csv(self, file, count):
        file.seek(0)
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        logger.info("Loading {} rows into '{}'".format(count, stream))

        with self.open_connection() as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(self.create_table_query(True))
                copy_sql = "COPY {} ({}) FROM STDIN WITH (FORMAT CSV)".format(
                    self.table_name(stream, True),
                    ', '.join(self.column_names())
                )
                logger.info(copy_sql)
                cur.copy_expert(
                    copy_sql,
                    file
                )
                if len(self.stream_schema_message['key_properties']) > 0:
                    cur.execute(self.update_from_temp_table())
                    logger.info(cur.statusmessage)
                cur.execute(self.insert_from_temp_table())
                logger.info(cur.statusmessage)
                cur.execute(self.drop_temp_table())

    def insert_from_temp_table(self):
        stream_schema_message = self.stream_schema_message
        columns = self.column_names()
        table = self.table_name(stream_schema_message['stream'], False)
        temp_table = self.table_name(stream_schema_message['stream'], True)

        if len(stream_schema_message['key_properties']) == 0:
            return """INSERT INTO {} ({})
                    (SELECT s.* FROM {} s)
                    """.format(
                table,
                ', '.join(columns),
                temp_table
            )

        return """INSERT INTO {} ({})
        (SELECT s.* FROM {} s LEFT OUTER JOIN {} t ON {} WHERE {})
        """.format(
            table,
            ', '.join(columns),
            temp_table,
            table,
            self.primary_key_condition('t'),
            self.primary_key_null_condition('t')
        )

    def update_from_temp_table(self):
        stream_schema_message = self.stream_schema_message
        columns = self.column_names()
        table = self.table_name(stream_schema_message['stream'], False)
        temp_table = self.table_name(stream_schema_message['stream'], True)
        return """UPDATE {} SET {} FROM {} s
        WHERE {}
        """.format(
            table,
            ', '.join(['{}=s.{}'.format(c, c) for c in columns]),
            temp_table,
            self.primary_key_condition(table)
        )

    def primary_key_condition(self, right_table):
        stream_schema_message = self.stream_schema_message
        names = primary_column_names(stream_schema_message)
        return ' AND '.join(['s.{} = {}.{}'.format(c, right_table, c) for c in names])

    def primary_key_null_condition(self, right_table):
        stream_schema_message = self.stream_schema_message
        names = primary_column_names(stream_schema_message)
        return ' AND '.join(['{}.{} is null'.format(right_table, c) for c in names])

    def drop_temp_table(self):
        stream_schema_message = self.stream_schema_message
        temp_table = self.table_name(stream_schema_message['stream'], True)
        return "DROP TABLE {}".format(temp_table)

    def column_names(self):
        return [safe_column_name(name) for name in self.flatten_schema]

    def create_table_query(self, is_temporary=False):
        stream_schema_message = self.stream_schema_message
        columns = [
            column_clause(
                name,
                schema,
                use_simple_column_type=self.use_simple_column_type,
            )
            for (name, schema) in self.flatten_schema.items()
        ]

        primary_key = ["PRIMARY KEY ({})".format(', '.join(primary_column_names(stream_schema_message)))] \
            if len(stream_schema_message['key_properties']) else []

        return 'CREATE {}TABLE {} ({})'.format(
            'TEMP ' if is_temporary else '',
            self.table_name(stream_schema_message['stream'], is_temporary),
            ', '.join(columns + primary_key)
        )

    def create_schema_if_not_exists(self):
        schema_name = self.target_config['schema']
        schema_rows = self.query(
            'SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s',
            (schema_name,)
        )

        if len(schema_rows) == 0:
            self.query("CREATE SCHEMA IF NOT EXISTS {}".format(schema_name))

    def get_tables(self):
        return self.query(
            'SELECT table_name FROM information_schema.tables WHERE table_schema = %s',
            (self.schema_name,)
        )

    def get_table_columns(self, table_name):
        return self.query("""SELECT column_name, data_type
      FROM information_schema.columns
      WHERE lower(table_name) = %s AND lower(table_schema) = %s""", (table_name.lower(), self.schema_name.lower()))

    def update_columns(self):
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        columns = self.get_table_columns(inflect_name(stream))
        columns_dict = {column['column_name'].lower(): column for column in columns}

        columns_to_add = [
            column_clause(
                name,
                properties_schema,
                use_simple_column_type=self.use_simple_column_type,
            )
            for (name, properties_schema) in self.flatten_schema.items()
            if name.lower() not in columns_dict
        ]

        for column in columns_to_add:
            self.add_column(column, stream)

        columns_to_replace = []
        for (name, properties_schema) in self.flatten_schema.items():
            name_lower = name.lower()
            col_type = column_type(properties_schema, use_simple=self.use_simple_column_type).lower()
            col_data_type = columns_dict[name_lower]['data_type'].lower()
            if name_lower in columns_dict and col_data_type != col_type and (
                # Do not drop the column if it used to not have a timezone, and we are adding one.
                # This is done so we can migrate all timestamps to have a timezone.
                not (col_data_type == 'timestamp without time zone' and col_type == 'timestamp with time zone')
            ):
                columns_to_replace.append(
                    (
                        safe_column_name(name),
                        column_clause(name, properties_schema, use_simple_column_type=self.use_simple_column_type)
                    )
                )

        for (column_name, column) in columns_to_replace:
            self.drop_column(column_name, stream)
            self.add_column(column, stream)

    def add_column(self, column, stream):
        add_column = "ALTER TABLE {} ADD COLUMN {}".format(self.table_name(stream, False), column)
        logger.info('Adding column: {}'.format(add_column))
        self.query(add_column)

    def drop_column(self, column_name, stream):
        drop_column = "ALTER TABLE {} DROP COLUMN {}".format(self.table_name(stream, False), column_name)
        logger.info('Dropping column: {}'.format(drop_column))
        self.query(drop_column)

    def sync_table(self):
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        stream = inflect_name(stream)
        found_tables = [table for table in (self.get_tables()) if table['table_name'].lower() == stream.lower()]
        if len(found_tables) == 0:
            query = self.create_table_query()
            logger.info("Table '{}' does not exist. Creating... {}".format(stream, query))
            self.query(query)
        else:
            logger.info("Table '{}' exists".format(stream))
            self.update_columns()
