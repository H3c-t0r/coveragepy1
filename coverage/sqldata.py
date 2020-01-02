# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Sqlite coverage data."""

# TODO: factor out dataop debugging to a wrapper class?
# TODO: make sure all dataop debugging is in place somehow

import collections
import datetime
import glob
import itertools
import os
import re
import sqlite3
import sys
import zlib

from coverage.backward import get_thread_id, iitems, to_bytes, to_string
from coverage.debug import NoDebugging, SimpleReprMixin, clipped_repr
from coverage.files import PathAliases
from coverage.misc import CoverageException, contract, file_be_gone, filename_suffix, isolate_module
from coverage.numbits import numbits_to_nums, numbits_union, nums_to_numbits
from coverage.version import __version__

os = isolate_module(os)

# If you change the schema, increment the SCHEMA_VERSION, and update the
# docs in docs/dbschema.rst also.

SCHEMA_VERSION = 7

# Schema versions:
# 1: Released in 5.0a2
# 2: Added contexts in 5.0a3.
# 3: Replaced line table with line_map table.
# 4: Changed line_map.bitmap to line_map.numbits.
# 5: Added foreign key declarations.
# 6: Key-value in meta.
# 7: line_map -> line_bits

SCHEMA = """\
CREATE TABLE coverage_schema (
    -- One row, to record the version of the schema in this db.
    version integer
);

CREATE TABLE meta (
    -- Key-value pairs, to record metadata about the data
    key text,
    value text,
    unique (key)
    -- Keys:
    --  'has_arcs' boolean      -- Is this data recording branches?
    --  'sys_argv' text         -- The coverage command line that recorded the data.
    --  'version' text          -- The version of coverage.py that made the file.
    --  'when' text             -- Datetime when the file was created.
);

CREATE TABLE file (
    -- A row per file measured.
    id integer primary key,
    path text,
    unique (path)
);

CREATE TABLE context (
    -- A row per context measured.
    id integer primary key,
    context text,
    unique (context)
);

CREATE TABLE line_bits (
    -- If recording lines, a row per context per file executed.
    -- All of the line numbers for that file/context are in one numbits.
    file_id integer,            -- foreign key to `file`.
    context_id integer,         -- foreign key to `context`.
    numbits blob,               -- see the numbits functions in coverage.numbits
    foreign key (file_id) references file (id),
    foreign key (context_id) references context (id),
    unique (file_id, context_id)
);

CREATE TABLE arc (
    -- If recording branches, a row per context per from/to line transition executed.
    file_id integer,            -- foreign key to `file`.
    context_id integer,         -- foreign key to `context`.
    fromno integer,             -- line number jumped from.
    tono integer,               -- line number jumped to.
    foreign key (file_id) references file (id),
    foreign key (context_id) references context (id),
    unique (file_id, context_id, fromno, tono)
);

CREATE TABLE tracer (
    -- A row per file indicating the tracer used for that file.
    file_id integer primary key,
    tracer text,
    foreign key (file_id) references file (id)
);
"""

class CoverageData(SimpleReprMixin):
    """Manages collected coverage data, including file storage.

    This class is the public supported API to the data that coverage.py
    collects during program execution.  It includes information about what code
    was executed. It does not include information from the analysis phase, to
    determine what lines could have been executed, or what lines were not
    executed.

    .. note::

        The data file is currently a SQLite database file, with a
        :ref:`documented schema <dbschema>`. The schema is subject to change
        though, so be careful about querying it directly. Use this API if you
        can to isolate yourself from changes.

    There are a number of kinds of data that can be collected:

    * **lines**: the line numbers of source lines that were executed.
      These are always available.

    * **arcs**: pairs of source and destination line numbers for transitions
      between source lines.  These are only available if branch coverage was
      used.

    * **file tracer names**: the module names of the file tracer plugins that
      handled each file in the data.

    Lines, arcs, and file tracer names are stored for each source file. File
    names in this API are case-sensitive, even on platforms with
    case-insensitive file systems.

    A data file either stores lines, or arcs, but not both.

    A data file is associated with the data when the :class:`CoverageData`
    is created, using the parameters `basename`, `suffix`, and `no_disk`. The
    base name can be queried with :meth:`base_filename`, and the actual file
    name being used is available from :meth:`data_filename`.

    To read an existing coverage.py data file, use :meth:`read`.  You can then
    access the line, arc, or file tracer data with :meth:`lines`, :meth:`arcs`,
    or :meth:`file_tracer`.

    The :meth:`has_arcs` method indicates whether arc data is available.  You
    can get a set of the files in the data with :meth:`measured_files`.  As
    with most Python containers, you can determine if there is any data at all
    by using this object as a boolean value.

    The contexts for each line in a file can be read with
    :meth:`contexts_by_lineno`.

    To limit querying to certain contexts, use :meth:`set_query_context` or
    :meth:`set_query_contexts`. These will narrow the focus of subsequent
    :meth:`lines`, :meth:`arcs`, and :meth:`contexts_by_lineno` calls. The set
    of all measured context names can be retrieved with
    :meth:`measured_contexts`.

    Most data files will be created by coverage.py itself, but you can use
    methods here to create data files if you like.  The :meth:`add_lines`,
    :meth:`add_arcs`, and :meth:`add_file_tracers` methods add data, in ways
    that are convenient for coverage.py.

    To record data for contexts, use :meth:`set_context` to set a context to
    be used for subsequent :meth:`add_lines` and :meth:`add_arcs` calls.

    To add a source file without any measured data, use :meth:`touch_file`.

    Write the data to its file with :meth:`write`.

    You can clear the data in memory with :meth:`erase`.  Two data collections
    can be combined by using :meth:`update` on one :class:`CoverageData`,
    passing it the other.

    Data in a :class:`CoverageData` can be serialized and deserialized with
    :meth:`dumps` and :meth:`loads`.

    """

    def __init__(self, basename=None, suffix=None, no_disk=False, warn=None, debug=None):
        """Create a :class:`CoverageData` object to hold coverage-measured data.

        Arguments:
            basename (str): the base name of the data file, defaulting to
                ".coverage".
            suffix (str or bool): has the same meaning as the `data_suffix`
                argument to :class:`coverage.Coverage`.
            no_disk (bool): if True, keep all data in memory, and don't
                write any disk file.
            warn: a warning callback function, accepting a warning message
                argument.
            debug: a debug callback function.

        """
        self._no_disk = no_disk
        self._basename = os.path.abspath(basename or ".coverage")
        self._suffix = suffix
        self._warn = warn
        self._debug = debug or NoDebugging()

        self._choose_filename()
        self._file_map = {}
        # Maps thread ids to SqliteDb objects.
        self._dbs = {}
        self._pid = os.getpid()

        # Are we in sync with the data file?
        self._have_used = False

        self._has_lines = False
        self._has_arcs = False

        self._current_context = None
        self._current_context_id = None
        self._query_context_ids = None

    def _choose_filename(self):
        """Set self._filename based on inited attributes."""
        if self._no_disk:
            self._filename = ":memory:"
        else:
            self._filename = self._basename
            suffix = filename_suffix(self._suffix)
            if suffix:
                self._filename += "." + suffix

    def _reset(self):
        """Reset our attributes."""
        if self._dbs:
            for db in self._dbs.values():
                db.close()
        self._dbs = {}
        self._file_map = {}
        self._have_used = False
        self._current_context_id = None

    def _create_db(self):
        """Create a db file that doesn't exist yet.

        Initializes the schema and certain metadata.
        """
        if self._debug.should('dataio'):
            self._debug.write("Creating data file {!r}".format(self._filename))
        self._dbs[get_thread_id()] = db = SqliteDb(self._filename, self._debug)
        with db:
            db.executescript(SCHEMA)
            db.execute("insert into coverage_schema (version) values (?)", (SCHEMA_VERSION,))
            db.executemany(
                "insert into meta (key, value) values (?, ?)",
                [
                    ('sys_argv', str(getattr(sys, 'argv', None))),
                    ('version', __version__),
                    ('when', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                ]
            )

    def _open_db(self):
        """Open an existing db file, and read its metadata."""
        if self._debug.should('dataio'):
            self._debug.write("Opening data file {!r}".format(self._filename))
        self._dbs[get_thread_id()] = SqliteDb(self._filename, self._debug)
        self._read_db()

    def _read_db(self):
        """Read the metadata from a database so that we are ready to use it."""
        with self._dbs[get_thread_id()] as db:
            try:
                schema_version, = db.execute("select version from coverage_schema").fetchone()
            except Exception as exc:
                raise CoverageException(
                    "Data file {!r} doesn't seem to be a coverage data file: {}".format(
                        self._filename, exc
                    )
                )
            else:
                if schema_version != SCHEMA_VERSION:
                    raise CoverageException(
                        "Couldn't use data file {!r}: wrong schema: {} instead of {}".format(
                            self._filename, schema_version, SCHEMA_VERSION
                        )
                    )

            for row in db.execute("select value from meta where key = 'has_arcs'"):
                self._has_arcs = bool(int(row[0]))
                self._has_lines = not self._has_arcs

            for path, file_id in db.execute("select path, id from file"):
                self._file_map[path] = file_id

    def _connect(self):
        """Get the SqliteDb object to use."""
        if get_thread_id() not in self._dbs:
            if os.path.exists(self._filename):
                self._open_db()
            else:
                self._create_db()
        return self._dbs[get_thread_id()]

    def __nonzero__(self):
        if (get_thread_id() not in self._dbs and not os.path.exists(self._filename)):
            return False
        try:
            with self._connect() as con:
                rows = con.execute("select * from file limit 1")
                return bool(list(rows))
        except CoverageException:
            return False

    __bool__ = __nonzero__

    @contract(returns='bytes')
    def dumps(self):
        """Serialize the current data to a byte string.

        The format of the serialized data is not documented. It is only
        suitable for use with :meth:`loads` in the same version of
        coverage.py.

        Returns:
            A byte string of serialized data.

        .. versionadded:: 5.0

        """
        if self._debug.should('dataio'):
            self._debug.write("Dumping data from data file {!r}".format(self._filename))
        with self._connect() as con:
            return b'z' + zlib.compress(to_bytes(con.dump()))

    @contract(data='bytes')
    def loads(self, data):
        """Deserialize data from :meth:`dumps`

        Use with a newly-created empty :class:`CoverageData` object.  It's
        undefined what happens if the object already has data in it.

        Arguments:
            data: A byte string of serialized data produced by :meth:`dumps`.

        .. versionadded:: 5.0

        """
        if self._debug.should('dataio'):
            self._debug.write("Loading data into data file {!r}".format(self._filename))
        if data[:1] != b'z':
            raise CoverageException(
                "Unrecognized serialization: {!r} (head of {} bytes)".format(data[:40], len(data))
                )
        script = to_string(zlib.decompress(data[1:]))
        self._dbs[get_thread_id()] = db = SqliteDb(self._filename, self._debug)
        with db:
            db.executescript(script)
        self._read_db()
        self._have_used = True

    def _file_id(self, filename, add=False):
        """Get the file id for `filename`.

        If filename is not in the database yet, add it if `add` is True.
        If `add` is not True, return None.
        """
        if filename not in self._file_map:
            if add:
                with self._connect() as con:
                    cur = con.execute("insert or replace into file (path) values (?)", (filename,))
                    self._file_map[filename] = cur.lastrowid
        return self._file_map.get(filename)

    def _context_id(self, context):
        """Get the id for a context."""
        assert context is not None
        self._start_using()
        with self._connect() as con:
            row = con.execute("select id from context where context = ?", (context,)).fetchone()
            if row is not None:
                return row[0]
            else:
                return None

    def set_context(self, context):
        """Set the current context for future :meth:`add_lines` etc.

        `context` is a str, the name of the context to use for the next data
        additions.  The context persists until the next :meth:`set_context`.

        .. versionadded:: 5.0

        """
        if self._debug.should('dataop'):
            self._debug.write("Setting context: %r" % (context,))
        self._current_context = context
        self._current_context_id = None

    def _set_context_id(self):
        """Use the _current_context to set _current_context_id."""
        context = self._current_context or ""
        context_id = self._context_id(context)
        if context_id is not None:
            self._current_context_id = context_id
        else:
            with self._connect() as con:
                cur = con.execute("insert into context (context) values (?)", (context,))
                self._current_context_id = cur.lastrowid

    def base_filename(self):
        """The base filename for storing data.

        .. versionadded:: 5.0

        """
        return self._basename

    def data_filename(self):
        """Where is the data stored?

        .. versionadded:: 5.0

        """
        return self._filename

    def add_lines(self, line_data):
        """Add measured line data.

        `line_data` is a dictionary mapping file names to dictionaries::

            { filename: { lineno: None, ... }, ...}

        """
        if self._debug.should('dataop'):
            self._debug.write("Adding lines: %d files, %d lines total" % (
                len(line_data), sum(len(lines) for lines in line_data.values())
            ))
        self._start_using()
        self._choose_lines_or_arcs(lines=True)
        if not line_data:
            return
        with self._connect() as con:
            self._set_context_id()
            for filename, linenos in iitems(line_data):
                linemap = nums_to_numbits(linenos)
                file_id = self._file_id(filename, add=True)
                query = "select numbits from line_bits where file_id = ? and context_id = ?"
                existing = list(con.execute(query, (file_id, self._current_context_id)))
                if existing:
                    linemap = numbits_union(linemap, existing[0][0])

                con.execute(
                    "insert or replace into line_bits "
                    " (file_id, context_id, numbits) values (?, ?, ?)",
                    (file_id, self._current_context_id, linemap),
                )

    def add_arcs(self, arc_data):
        """Add measured arc data.

        `arc_data` is a dictionary mapping file names to dictionaries::

            { filename: { (l1,l2): None, ... }, ...}

        """
        if self._debug.should('dataop'):
            self._debug.write("Adding arcs: %d files, %d arcs total" % (
                len(arc_data), sum(len(arcs) for arcs in arc_data.values())
            ))
        self._start_using()
        self._choose_lines_or_arcs(arcs=True)
        if not arc_data:
            return
        with self._connect() as con:
            self._set_context_id()
            for filename, arcs in iitems(arc_data):
                file_id = self._file_id(filename, add=True)
                data = [(file_id, self._current_context_id, fromno, tono) for fromno, tono in arcs]
                con.executemany(
                    "insert or ignore into arc "
                    "(file_id, context_id, fromno, tono) values (?, ?, ?, ?)",
                    data,
                )

    def _choose_lines_or_arcs(self, lines=False, arcs=False):
        """Force the data file to choose between lines and arcs."""
        assert lines or arcs
        assert not (lines and arcs)
        if lines and self._has_arcs:
            raise CoverageException("Can't add lines to existing arc data")
        if arcs and self._has_lines:
            raise CoverageException("Can't add arcs to existing line data")
        if not self._has_arcs and not self._has_lines:
            self._has_lines = lines
            self._has_arcs = arcs
            with self._connect() as con:
                con.execute(
                    "insert into meta (key, value) values (?, ?)",
                    ('has_arcs', str(int(arcs)))
                )

    def add_file_tracers(self, file_tracers):
        """Add per-file plugin information.

        `file_tracers` is { filename: plugin_name, ... }

        """
        if self._debug.should('dataop'):
            self._debug.write("Adding file tracers: %d files" % (len(file_tracers),))
        if not file_tracers:
            return
        self._start_using()
        with self._connect() as con:
            for filename, plugin_name in iitems(file_tracers):
                file_id = self._file_id(filename)
                if file_id is None:
                    raise CoverageException(
                        "Can't add file tracer data for unmeasured file '%s'" % (filename,)
                    )

                existing_plugin = self.file_tracer(filename)
                if existing_plugin:
                    if existing_plugin != plugin_name:
                        raise CoverageException(
                            "Conflicting file tracer name for '%s': %r vs %r" % (
                                filename, existing_plugin, plugin_name,
                            )
                        )
                elif plugin_name:
                    con.execute(
                        "insert into tracer (file_id, tracer) values (?, ?)",
                        (file_id, plugin_name)
                    )

    def touch_file(self, filename, plugin_name=""):
        """Ensure that `filename` appears in the data, empty if needed.

        `plugin_name` is the name of the plugin responsible for this file. It is used
        to associate the right filereporter, etc.
        """
        if self._debug.should('dataop'):
            self._debug.write("Touching %r" % (filename,))
        self._start_using()
        if not self._has_arcs and not self._has_lines:
            raise CoverageException("Can't touch files in an empty CoverageData")

        self._file_id(filename, add=True)
        if plugin_name:
            # Set the tracer for this file
            self.add_file_tracers({filename: plugin_name})

    def update(self, other_data, aliases=None):
        """Update this data with data from several other :class:`CoverageData` instances.

        If `aliases` is provided, it's a `PathAliases` object that is used to
        re-map paths to match the local machine's.
        """
        if self._debug.should('dataop'):
            self._debug.write("Updating with data from %r" % (
                getattr(other_data, '_filename', '???'),
            ))
        if self._has_lines and other_data._has_arcs:
            raise CoverageException("Can't combine arc data with line data")
        if self._has_arcs and other_data._has_lines:
            raise CoverageException("Can't combine line data with arc data")

        aliases = aliases or PathAliases()

        # Force the database we're writing to to exist before we start nesting
        # contexts.
        self._start_using()

        # Collector for all arcs, lines and tracers
        other_data.read()
        with other_data._connect() as conn:
            # Get files data.
            cur = conn.execute('select path from file')
            files = {path: aliases.map(path) for (path,) in cur}
            cur.close()

            # Get contexts data.
            cur = conn.execute('select context from context')
            contexts = [context for (context,) in cur]
            cur.close()

            # Get arc data.
            cur = conn.execute(
                'select file.path, context.context, arc.fromno, arc.tono '
                'from arc '
                'inner join file on file.id = arc.file_id '
                'inner join context on context.id = arc.context_id'
            )
            arcs = [(files[path], context, fromno, tono) for (path, context, fromno, tono) in cur]
            cur.close()

            # Get line data.
            cur = conn.execute(
                'select file.path, context.context, line_bits.numbits '
                'from line_bits '
                'inner join file on file.id = line_bits.file_id '
                'inner join context on context.id = line_bits.context_id'
                )
            lines = {
                (files[path], context): numbits
                for (path, context, numbits) in cur
                }
            cur.close()

            # Get tracer data.
            cur = conn.execute(
                'select file.path, tracer '
                'from tracer '
                'inner join file on file.id = tracer.file_id'
            )
            tracers = {files[path]: tracer for (path, tracer) in cur}
            cur.close()

        with self._connect() as conn:
            conn.con.isolation_level = 'IMMEDIATE'

            # Get all tracers in the DB. Files not in the tracers are assumed
            # to have an empty string tracer. Since Sqlite does not support
            # full outer joins, we have to make two queries to fill the
            # dictionary.
            this_tracers = {path: '' for path, in conn.execute('select path from file')}
            this_tracers.update({
                aliases.map(path): tracer
                for path, tracer in conn.execute(
                    'select file.path, tracer from tracer '
                    'inner join file on file.id = tracer.file_id'
                )
            })

            # Create all file and context rows in the DB.
            conn.executemany(
                'insert or ignore into file (path) values (?)',
                ((file,) for file in files.values())
            )
            file_ids = {
                path: id
                for id, path in conn.execute('select id, path from file')
            }
            conn.executemany(
                'insert or ignore into context (context) values (?)',
                ((context,) for context in contexts)
            )
            context_ids = {
                context: id
                for id, context in conn.execute('select id, context from context')
            }

            # Prepare tracers and fail, if a conflict is found.
            # tracer_paths is used to ensure consistency over the tracer data
            # and tracer_map tracks the tracers to be inserted.
            tracer_map = {}
            for path in files.values():
                this_tracer = this_tracers.get(path)
                other_tracer = tracers.get(path, '')
                # If there is no tracer, there is always the None tracer.
                if this_tracer is not None and this_tracer != other_tracer:
                    raise CoverageException(
                        "Conflicting file tracer name for '%s': %r vs %r" % (
                            path, this_tracer, other_tracer
                        )
                    )
                tracer_map[path] = other_tracer

            # Prepare arc and line rows to be inserted by converting the file
            # and context strings with integer ids. Then use the efficient
            # `executemany()` to insert all rows at once.
            arc_rows = (
                (file_ids[file], context_ids[context], fromno, tono)
                for file, context, fromno, tono in arcs
            )

            # Get line data.
            cur = conn.execute(
                'select file.path, context.context, line_bits.numbits '
                'from line_bits '
                'inner join file on file.id = line_bits.file_id '
                'inner join context on context.id = line_bits.context_id'
                )
            for path, context, numbits in cur:
                key = (aliases.map(path), context)
                if key in lines:
                    numbits = numbits_union(lines[key], numbits)
                lines[key] = numbits
            cur.close()

            if arcs:
                self._choose_lines_or_arcs(arcs=True)

                # Write the combined data.
                conn.executemany(
                    'insert or ignore into arc '
                    '(file_id, context_id, fromno, tono) values (?, ?, ?, ?)',
                    arc_rows
                )

            if lines:
                self._choose_lines_or_arcs(lines=True)
                conn.execute("delete from line_bits")
                conn.executemany(
                    "insert into line_bits "
                    "(file_id, context_id, numbits) values (?, ?, ?)",
                    [
                        (file_ids[file], context_ids[context], numbits)
                        for (file, context), numbits in lines.items()
                    ]
                )
            conn.executemany(
                'insert or ignore into tracer (file_id, tracer) values (?, ?)',
                ((file_ids[filename], tracer) for filename, tracer in tracer_map.items())
            )

        # Update all internal cache data.
        self._reset()
        self.read()

    def erase(self, parallel=False):
        """Erase the data in this object.

        If `parallel` is true, then also deletes data files created from the
        basename by parallel-mode.

        """
        self._reset()
        if self._no_disk:
            return
        if self._debug.should('dataio'):
            self._debug.write("Erasing data file {!r}".format(self._filename))
        file_be_gone(self._filename)
        if parallel:
            data_dir, local = os.path.split(self._filename)
            localdot = local + '.*'
            pattern = os.path.join(os.path.abspath(data_dir), localdot)
            for filename in glob.glob(pattern):
                if self._debug.should('dataio'):
                    self._debug.write("Erasing parallel data file {!r}".format(filename))
                file_be_gone(filename)

    def read(self):
        """Start using an existing data file."""
        with self._connect():       # TODO: doesn't look right
            self._have_used = True

    def write(self):
        """Ensure the data is written to the data file."""
        pass

    def _start_using(self):
        """Call this before using the database at all."""
        if self._pid != os.getpid():
            # Looks like we forked! Have to start a new data file.
            self._reset()
            self._choose_filename()
            self._pid = os.getpid()
        if not self._have_used:
            self.erase()
        self._have_used = True

    def has_arcs(self):
        """Does the database have arcs (True) or lines (False)."""
        return bool(self._has_arcs)

    def measured_files(self):
        """A set of all files that had been measured."""
        return set(self._file_map)

    def measured_contexts(self):
        """A set of all contexts that have been measured.

        .. versionadded:: 5.0

        """
        self._start_using()
        with self._connect() as con:
            contexts = set(row[0] for row in con.execute("select distinct(context) from context"))
        return contexts

    def file_tracer(self, filename):
        """Get the plugin name of the file tracer for a file.

        Returns the name of the plugin that handles this file.  If the file was
        measured, but didn't use a plugin, then "" is returned.  If the file
        was not measured, then None is returned.

        """
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            row = con.execute("select tracer from tracer where file_id = ?", (file_id,)).fetchone()
            if row is not None:
                return row[0] or ""
            return ""   # File was measured, but no tracer associated.

    def set_query_context(self, context):
        """Set a context for subsequent querying.

        The next :meth:`lines`, :meth:`arcs`, or :meth:`contexts_by_lineno`
        calls will be limited to only one context.  `context` is a string which
        must match a context exactly.  If it does not, no exception is raised,
        but queries will return no data.

        .. versionadded:: 5.0

        """
        self._start_using()
        with self._connect() as con:
            cur = con.execute("select id from context where context = ?", (context,))
            self._query_context_ids = [row[0] for row in cur.fetchall()]

    def set_query_contexts(self, contexts):
        """Set a number of contexts for subsequent querying.

        The next :meth:`lines`, :meth:`arcs`, or :meth:`contexts_by_lineno`
        calls will be limited to the specified contexts.  `contexts` is a list
        of Python regular expressions.  Contexts will be matched using
        :func:`re.search <python:re.search>`.  Data will be included in query
        results if they are part of any of the contexts matched.

        .. versionadded:: 5.0

        """
        self._start_using()
        if contexts:
            with self._connect() as con:
                context_clause = ' or '.join(['context regexp ?'] * len(contexts))
                cur = con.execute("select id from context where " + context_clause, contexts)
                self._query_context_ids = [row[0] for row in cur.fetchall()]
        else:
            self._query_context_ids = None

    def lines(self, filename):
        """Get the list of lines executed for a file.

        If the file was not measured, returns None.  A file might be measured,
        and have no lines executed, in which case an empty list is returned.

        If the file was executed, returns a list of integers, the line numbers
        executed in the file. The list is in no particular order.

        """
        self._start_using()
        if self.has_arcs():
            arcs = self.arcs(filename)
            if arcs is not None:
                all_lines = itertools.chain.from_iterable(arcs)
                return list(set(l for l in all_lines if l > 0))

        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            else:
                query = "select numbits from line_bits where file_id = ?"
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ', '.join('?' * len(self._query_context_ids))
                    query += " and context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                bitmaps = list(con.execute(query, data))
                nums = set()
                for row in bitmaps:
                    nums.update(numbits_to_nums(row[0]))
                return list(nums)

    def arcs(self, filename):
        """Get the list of arcs executed for a file.

        If the file was not measured, returns None.  A file might be measured,
        and have no arcs executed, in which case an empty list is returned.

        If the file was executed, returns a list of 2-tuples of integers. Each
        pair is a starting line number and an ending line number for a
        transition from one line to another. The list is in no particular
        order.

        Negative numbers have special meaning.  If the starting line number is
        -N, it represents an entry to the code object that starts at line N.
        If the ending ling number is -N, it's an exit from the code object that
        starts at line N.

        """
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            else:
                query = "select distinct fromno, tono from arc where file_id = ?"
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ', '.join('?' * len(self._query_context_ids))
                    query += " and context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                arcs = con.execute(query, data)
                return list(arcs)

    def contexts_by_lineno(self, filename):
        """Get the contexts for each line in a file.

        Returns:
            A dict mapping line numbers to a list of context names.

        .. versionadded:: 5.0

        """
        lineno_contexts_map = collections.defaultdict(list)
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return lineno_contexts_map
            if self.has_arcs():
                query = (
                    "select arc.fromno, arc.tono, context.context "
                    "from arc, context "
                    "where arc.file_id = ? and arc.context_id = context.id"
                )
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ', '.join('?' * len(self._query_context_ids))
                    query += " and arc.context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                for fromno, tono, context in con.execute(query, data):
                    if context not in lineno_contexts_map[fromno]:
                        lineno_contexts_map[fromno].append(context)
                    if context not in lineno_contexts_map[tono]:
                        lineno_contexts_map[tono].append(context)
            else:
                query = (
                    "select l.numbits, c.context from line_bits l, context c "
                    "where l.context_id = c.id "
                    "and file_id = ?"
                    )
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ', '.join('?' * len(self._query_context_ids))
                    query += " and l.context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                for numbits, context in con.execute(query, data):
                    for lineno in numbits_to_nums(numbits):
                        lineno_contexts_map[lineno].append(context)
        return lineno_contexts_map

    @classmethod
    def sys_info(cls):
        """Our information for `Coverage.sys_info`.

        Returns a list of (key, value) pairs.

        """
        return [
            ('sqlite3_version', sqlite3.version),
            ('sqlite3_sqlite_version', sqlite3.sqlite_version),
        ]


class SqliteDb(SimpleReprMixin):
    """A simple abstraction over a SQLite database.

    Use as a context manager, then you can use it like a
    :class:`python:sqlite3.Connection` object::

        with SqliteDb(filename, debug=True) as db:
            db.execute("insert into schema (version) values (?)", (SCHEMA_VERSION,))

    """
    def __init__(self, filename, debug):
        self.debug = debug if debug.should('sql') else None
        self.filename = filename
        self.nest = 0
        self.con = None
        # SQLite on Windows on py2 won't open a file if the filename argument
        # has non-ascii characters in it.  Opening a relative file name avoids
        # a problem if the current directory has non-ascii.
        self.connect_filename = os.path.relpath(self.filename)

    def _connect(self):
        """Connect to the db and do universal initialization."""
        if self.con is not None:
            return

        # It can happen that Python switches threads while the tracer writes
        # data. The second thread will also try to write to the data,
        # effectively causing a nested context. However, given the idempotent
        # nature of the tracer operations, sharing a connection among threads
        # is not a problem.
        if self.debug:
            self.debug.write("Connecting to {!r}".format(self.filename))
        self.con = sqlite3.connect(self.connect_filename, check_same_thread=False)
        self.con.create_function('REGEXP', 2, _regexp)

        # This pragma makes writing faster. It disables rollbacks, but we never need them.
        # PyPy needs the .close() calls here, or sqlite gets twisted up:
        # https://bitbucket.org/pypy/pypy/issues/2872/default-isolation-mode-is-different-on
        self.execute("pragma journal_mode=off").close()
        # This pragma makes writing faster.
        self.execute("pragma synchronous=off").close()

    def close(self):
        """If needed, close the connection."""
        if self.con is not None and self.filename != ":memory:":
            self.con.close()
            self.con = None

    def __enter__(self):
        if self.nest == 0:
            self._connect()
            self.con.__enter__()
        self.nest += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.nest -= 1
        if self.nest == 0:
            self.con.__exit__(exc_type, exc_value, traceback)
            self.close()

    def execute(self, sql, parameters=()):
        """Same as :meth:`python:sqlite3.Connection.execute`."""
        if self.debug:
            tail = " with {!r}".format(parameters) if parameters else ""
            self.debug.write("Executing {!r}{}".format(sql, tail))
        try:
            return self.con.execute(sql, parameters)
        except sqlite3.Error as exc:
            raise CoverageException("Couldn't use data file {!r}: {}".format(self.filename, exc))

    def executemany(self, sql, data):
        """Same as :meth:`python:sqlite3.Connection.executemany`."""
        if self.debug:
            data = list(data)
            self.debug.write("Executing many {!r} with {} rows".format(sql, len(data)))
        return self.con.executemany(sql, data)

    def executescript(self, script):
        """Same as :meth:`python:sqlite3.Connection.executescript`."""
        if self.debug:
            self.debug.write("Executing script with {} chars: {}".format(
                len(script), clipped_repr(script, 100),
            ))
        self.con.executescript(script)

    def dump(self):
        """Return a multi-line string, the SQL dump of the database."""
        return "\n".join(self.con.iterdump())


def _regexp(text, pattern):
    """A regexp function for SQLite."""
    return re.search(text, pattern) is not None
