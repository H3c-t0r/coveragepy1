# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Command-line support for coverage.py."""


import argparse
import glob
import os.path
import shlex
import sys
import textwrap
import traceback

import coverage
from coverage import Coverage
from coverage import env
from coverage.collector import CTracer
from coverage.data import line_counts
from coverage.debug import info_formatter, info_header, short_stack
from coverage.exceptions import BaseCoverageException, ExceptionDuringRun, NoSource
from coverage.execfile import PyRunner
from coverage.results import Numbers, should_fail_under


class Argument:
    """Store the definition of an argparse argument"""
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def add_to(self, parser: argparse.ArgumentParser):
        parser.add_argument(*self.args, **self.kwargs)


class Args:
    """A namespace class for individual options we'll build parsers from."""

    @classmethod
    def add_to(cls, parser: argparse.ArgumentParser, *args: str):
        for arg_name in args:
            arg = getattr(cls, arg_name)
            arg.add_to(parser)

    append = Argument(
        '-a', '--append', action='store_true',
        help="Append coverage data to .coverage, otherwise it starts clean each time.",
    )
    keep = Argument(
        '--keep', action='store_true',
        help="Keep original coverage files, otherwise they are deleted.",
    )
    branch = Argument(
        '--branch', action='store_true',
        help="Measure branch coverage in addition to statement coverage.",
    )
    CONCURRENCY_CHOICES = [
        "thread", "gevent", "greenlet", "eventlet", "multiprocessing",
    ]
    concurrency = Argument(
        '--concurrency', action='store', metavar="LIB",
        choices=CONCURRENCY_CHOICES,
        help=(
            "Properly measure code using a concurrency library. "
            "Valid values are: {}."
        ).format(", ".join(CONCURRENCY_CHOICES)),
    )
    context = Argument(
        '--context', action='store', metavar="LABEL",
        help="The context label to record for this coverage run.",
    )
    directory = Argument(
        '-d', '--directory', action='store', metavar="DIR",
        help="Write the output files to DIR.",
    )
    fail_under = Argument(
        '--fail-under', action='store', metavar="MIN", type=float,
        help="Exit with a status of 2 if the total coverage is less than MIN.",
    )
    help = Argument(
        '-h', '--help', action='store_true',
        help="Get help on this command.",
    )
    ignore_errors = Argument(
        '-i', '--ignore-errors', action='store_true',
        help="Ignore errors while reading source files.",
    )
    include = Argument(
        '--include', action='store',
        metavar="PAT1,PAT2,...",
        help=(
            "Include only files whose paths match one of these patterns. "
            "Accepts shell-style wildcards, which must be quoted."
        ),
    )
    pylib = Argument(
        '-L', '--pylib', action='store_true',
        help=(
            "Measure coverage even inside the Python installed library, "
            "which isn't done by default."
        ),
    )
    sort = Argument(
        '--sort', action='store', metavar='COLUMN',
        help="Sort the report by the named column: name, stmts, miss, branch, brpart, or cover. "
             "Default is name."
    )
    show_missing = Argument(
        '-m', '--show-missing', action='store_true',
        help="Show line numbers of statements in each module that weren't executed.",
    )
    skip_covered = Argument(
        '--skip-covered', action='store_true',
        help="Skip files with 100% coverage.",
    )
    no_skip_covered = Argument(
        '--no-skip-covered', action='store_false', dest='skip_covered',
        help="Disable --skip-covered.",
    )
    skip_empty = Argument(
        '--skip-empty', action='store_true',
        help="Skip files with no code.",
    )
    show_contexts = Argument(
        '--show-contexts', action='store_true',
        help="Show contexts for covered lines.",
    )
    omit = Argument(
        '--omit', action='store',
        metavar="PAT1,PAT2,...",
        help=(
            "Omit files whose paths match one of these patterns. "
            "Accepts shell-style wildcards, which must be quoted."
        ),
    )
    contexts = Argument(
        '--contexts', action='store',
        metavar="REGEX1,REGEX2,...",
        help=(
            "Only display data from lines covered in the given contexts. "
            "Accepts Python regexes, which must be quoted."
        ),
    )
    output_xml = Argument(
        '-o', action='store', dest="outfile",
        metavar="OUTFILE",
        help="Write the XML report to this file. Defaults to 'coverage.xml'",
    )
    output_json = Argument(
        '-o', action='store', dest="outfile",
        metavar="OUTFILE",
        help="Write the JSON report to this file. Defaults to 'coverage.json'",
    )
    json_pretty_print = Argument(
        '--pretty-print', action='store_true',
        help="Format the JSON for human readers.",
    )
    parallel_mode = Argument(
        '-p', '--parallel-mode', action='store_true',
        help=(
            "Append the machine name, process id and random number to the "
            ".coverage data file name to simplify collecting data from "
            "many processes."
        ),
    )
    module = Argument(
        '-m', '--module', action='store_true',
        help=(
            "<pyfile> is an importable Python module, not a script path, "
            "to be run as 'python -m' would run it."
        ),
    )
    precision = Argument(
        '--precision', action='store', metavar='N', type=int,
        help=(
            "Number of digits after the decimal point to display for "
            "reported coverage percentages."
        ),
    )
    source = Argument(
        '--source', action='store', metavar="SRC1,SRC2,...",
        help="A list of directories or importable names of code to measure.",
    )
    timid = Argument(
        '--timid', action='store_true',
        help=(
            "Use a simpler but slower trace method. Try this if you get "
            "seemingly impossible results!"
        ),
    )
    title = Argument(
        '--title', action='store', metavar="TITLE",
        help="A text string to use as the title on the HTML.",
    )


def version_string():
    if CTracer is not None:
        extension_modifier = 'with C extension'
    else:
        extension_modifier = 'without C extension'

    return f"Coverage.py, version {coverage.__version__} {extension_modifier}"


def make_parser():
    version = version_string()
    description = (
        "Measure, collect, and report on code coverage in Python programs."
    )
    help = """Use "%(prog)s help <command>" for detailed help on any command."""
    doc = f"""Full documentation is at {coverage.__url__}"""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{version}\n{description}",
        epilog=f"{help}\n{doc}"
    )
    parser.add_argument(
        '--debug', action='store', metavar="OPTS",
        help="Debug options, separated by commas. [env: COVERAGE_DEBUG]"
    )
    parser.add_argument(
        '--rcfile', action='store',
        help=(
            "Specify configuration file. "
            "By default '.coveragerc', 'setup.cfg', 'tox.ini', and "
            "'pyproject.toml' are tried. [env: COVERAGE_RCFILE]"
        ),
    )
    parser.add_argument('--version', action='version', version=version)

    # TODO: required not supported in py3.6
    subparsers = parser.add_subparsers(title="Commands", dest="action")

    annotate = subparsers.add_parser(
        "annotate",
        help="Annotate source files with execution information.",
        description=(
            "Make annotated copies of the given files, marking statements that are executed "
            "with > and statements that are missed with !."
        ),
    )
    Args.add_to(annotate, "directory", "ignore_errors", "include", "omit")

    combine = subparsers.add_parser(
        "combine",
        help="Combine a number of data files.",
        description=(
            "Combine data from multiple coverage files collected "
            "with 'run -p'.  The combined results are written to a single "
            "file representing the union of the data. The positional "
            "arguments are data files or directories containing data files. "
            "If no paths are provided, data files in the default data file's "
            "directory are combined."
        ),
    )
    Args.add_to(combine, "append", "keep")

    subparsers.add_parser(
        "debug",
        help="Display information about the internals of coverage.py",
        description=(
            "Display information about the internals of coverage.py, "
            "for diagnosing problems. "
            "Topics are: "
                "'data' to show a summary of the collected data; "
                "'sys' to show installation information; "
                "'config' to show the configuration; "
                "'premain' to show what is calling coverage."
        ),
    )

    subparsers.add_parser(
        "erase",
        help="Erase previously collected coverage data.",
    )
    subparsers.add_parser(
        "help",
        help="Get help on using coverage.py.",
    )
    html = subparsers.add_parser(
        "html",
        help="Create an HTML report.",
        description=(
            "Create an HTML report of the coverage of the files.  "
            "Each file gets its own page, with the source decorated to show "
            "executed, excluded, and missed lines."
        )
    )
    Args.add_to(
        html,
        "contexts", "directory", "fail_under", "ignore_errors",
        "include", "omit", "precision", "show_contexts", "skip_covered",
        "no_skip_covered", "skip_empty", "title"
    )

    json = subparsers.add_parser(
        "json",
        help="Create a JSON report of coverage results.",
    )
    Args.add_to(
        json,
        "contexts", "fail_under", "ignore_errors", "include", "omit",
        "output_json", "json_pretty_print", "show_contexts"
    )

    report = subparsers.add_parser(
        "report",
        help="Report coverage stats on modules.",
    )
    Args.add_to(
        report,
        "contexts", "fail_under", "ignore_errors", "include", "omit",
        "precision", "sort", "show_missing", "skip_covered",
        "no_skip_covered", "skip_empty"
    )

    run = subparsers.add_parser(
        "run",
        help="Run a Python program and measure code execution.",
    )
    Args.add_to(
        run,
        "append", "branch", "concurrency", "context", "include", "module",
        "omit", "pylib", "parallel_mode", "source", "timid"
    )

    xml = subparsers.add_parser(
        "xml",
        help="Create an XML report of coverage results.",
    )
    Args.add_to(
        xml,
        "fail_under", "ignore_errors", "include", "omit", "output_xml",
        "skip_empty"
    )

    return parser


OK, ERR, FAIL_UNDER = 0, 1, 2


class CoverageScript:
    """The command-line interface to coverage.py."""

    def __init__(self):
        self.global_option = False
        self.coverage = None

    def command_line(self, argv):
        """The bulk of the command line interface to coverage.py.

        `argv` is the argument list to process.

        Returns 0 if all is well, 1 if something went wrong.

        """
        # Collect the command-line options.
        if not argv:
            show_help(topic='minimum_help')
            return OK
        parser = make_parser()

        options = parser.parse_args()

        # Handle help and version.
        if self.do_help(options, args, parser):
            return OK

        # Listify the list options.
        source = unshell_list(options.source)
        omit = unshell_list(options.omit)
        include = unshell_list(options.include)
        debug = unshell_list(options.debug)
        contexts = unshell_list(options.contexts)

        # Do something.
        self.coverage = Coverage(
            data_suffix=options.parallel_mode,
            cover_pylib=options.pylib,
            timid=options.timid,
            branch=options.branch,
            config_file=options.rcfile,
            source=source,
            omit=omit,
            include=include,
            debug=debug,
            concurrency=options.concurrency,
            check_preimported=True,
            context=options.context,
            )

        if options.action == "debug":
            return self.do_debug(args)

        elif options.action == "erase":
            self.coverage.erase()
            return OK

        elif options.action == "run":
            return self.do_run(options, args)

        elif options.action == "combine":
            if options.append:
                self.coverage.load()
            data_dirs = args or None
            self.coverage.combine(data_dirs, strict=True, keep=bool(options.keep))
            self.coverage.save()
            return OK

        # Remaining actions are reporting, with some common options.
        report_args = dict(
            morfs=unglob_args(args),
            ignore_errors=options.ignore_errors,
            omit=omit,
            include=include,
            contexts=contexts,
            )

        # We need to be able to import from the current directory, because
        # plugins may try to, for example, to read Django settings.
        sys.path.insert(0, '')

        self.coverage.load()

        total = None
        if options.action == "report":
            total = self.coverage.report(
                show_missing=options.show_missing,
                skip_covered=options.skip_covered,
                skip_empty=options.skip_empty,
                precision=options.precision,
                sort=options.sort,
                **report_args
                )
        elif options.action == "annotate":
            self.coverage.annotate(directory=options.directory, **report_args)
        elif options.action == "html":
            total = self.coverage.html_report(
                directory=options.directory,
                title=options.title,
                skip_covered=options.skip_covered,
                skip_empty=options.skip_empty,
                show_contexts=options.show_contexts,
                precision=options.precision,
                **report_args
                )
        elif options.action == "xml":
            outfile = options.outfile
            total = self.coverage.xml_report(
                outfile=outfile, skip_empty=options.skip_empty,
                **report_args
                )
        elif options.action == "json":
            outfile = options.outfile
            total = self.coverage.json_report(
                outfile=outfile,
                pretty_print=options.pretty_print,
                show_contexts=options.show_contexts,
                **report_args
            )

        if total is not None:
            # Apply the command line fail-under options, and then use the config
            # value, so we can get fail_under from the config file.
            if options.fail_under is not None:
                self.coverage.set_option("report:fail_under", options.fail_under)

            fail_under = self.coverage.get_option("report:fail_under")
            precision = self.coverage.get_option("report:precision")
            if should_fail_under(total, fail_under, precision):
                msg = "total of {total} is less than fail-under={fail_under:.{p}f}".format(
                    total=Numbers(precision=precision).display_covered(total),
                    fail_under=fail_under,
                    p=precision,
                )
                print("Coverage failure:", msg)
                return FAIL_UNDER

        return OK

    def do_help(self, options, args, parser):
        """Deal with help requests.

        Return True if it handled the request, False if not.

        """
        # Handle help.
        if options.help:
            if self.global_option:
                show_help(topic='help')
            else:
                show_help(parser=parser)
            return True

        if options.action == "help":
            if args:
                for a in args:
                    parser = CMDS.get(a)
                    if parser:
                        show_help(parser=parser)
                    else:
                        show_help(topic=a)
            else:
                show_help(topic='help')
            return True

        # Handle version.
        if options.version:
            show_help(topic='version')
            return True

        return False

    def do_run(self, options, args):
        """Implementation of 'coverage run'."""

        if not args:
            if options.module:
                # Specified -m with nothing else.
                show_help("No module specified for -m")
                return ERR
            command_line = self.coverage.get_option("run:command_line")
            if command_line is not None:
                args = shlex.split(command_line)
                if args and args[0] == "-m":
                    options.module = True
                    args = args[1:]
        if not args:
            show_help("Nothing to do.")
            return ERR

        if options.append and self.coverage.get_option("run:parallel"):
            show_help("Can't append to data files in parallel mode.")
            return ERR

        if options.concurrency == "multiprocessing":
            # Can't set other run-affecting command line options with
            # multiprocessing.
            for opt_name in ['branch', 'include', 'omit', 'pylib', 'source', 'timid']:
                # As it happens, all of these options have no default, meaning
                # they will be None if they have not been specified.
                if getattr(options, opt_name) is not None:
                    show_help(
                        "Options affecting multiprocessing must only be specified "
                        "in a configuration file.\n"
                        "Remove --{} from the command line.".format(opt_name)
                    )
                    return ERR

        runner = PyRunner(args, as_module=bool(options.module))
        runner.prepare()

        if options.append:
            self.coverage.load()

        # Run the script.
        self.coverage.start()
        code_ran = True
        try:
            runner.run()
        except NoSource:
            code_ran = False
            raise
        finally:
            self.coverage.stop()
            if code_ran:
                self.coverage.save()

        return OK

    def do_debug(self, args):
        """Implementation of 'coverage debug'."""

        if not args:
            show_help("What information would you like: config, data, sys, premain?")
            return ERR

        for info in args:
            if info == 'sys':
                sys_info = self.coverage.sys_info()
                print(info_header("sys"))
                for line in info_formatter(sys_info):
                    print(f" {line}")
            elif info == 'data':
                self.coverage.load()
                data = self.coverage.get_data()
                print(info_header("data"))
                print(f"path: {data.data_filename()}")
                if data:
                    print(f"has_arcs: {data.has_arcs()!r}")
                    summary = line_counts(data, fullpath=True)
                    filenames = sorted(summary.keys())
                    print(f"\n{len(filenames)} files:")
                    for f in filenames:
                        line = f"{f}: {summary[f]} lines"
                        plugin = data.file_tracer(f)
                        if plugin:
                            line += f" [{plugin}]"
                        print(line)
                else:
                    print("No data collected")
            elif info == 'config':
                print(info_header("config"))
                config_info = self.coverage.config.__dict__.items()
                for line in info_formatter(config_info):
                    print(f" {line}")
            elif info == "premain":
                print(info_header("premain"))
                print(short_stack())
            else:
                show_help(f"Don't know what you mean by {info!r}")
                return ERR

        return OK


def unshell_list(s):
    """Turn a command-line argument into a list."""
    if not s:
        return None
    if env.WINDOWS:
        # When running coverage.py as coverage.exe, some of the behavior
        # of the shell is emulated: wildcards are expanded into a list of
        # file names.  So you have to single-quote patterns on the command
        # line, but (not) helpfully, the single quotes are included in the
        # argument, so we have to strip them off here.
        s = s.strip("'")
    return s.split(',')


def unglob_args(args):
    """Interpret shell wildcards for platforms that need it."""
    if env.WINDOWS:
        globbed = []
        for arg in args:
            if '?' in arg or '*' in arg:
                globbed.extend(glob.glob(arg))
            else:
                globbed.append(arg)
        args = globbed
    return args


HELP_TOPICS = {
    'help': """\
        Coverage.py, version {__version__} {extension_modifier}
        Measure, collect, and report on code coverage in Python programs.

        usage: {program_name} <command> [options] [args]

        Commands:
            annotate    Annotate source files with execution information.
            combine     Combine a number of data files.
            debug       Display information about the internals of coverage.py
            erase       Erase previously collected coverage data.
            help        Get help on using coverage.py.
            html        Create an HTML report.
            json        Create a JSON report of coverage results.
            report      Report coverage stats on modules.
            run         Run a Python program and measure code execution.
            xml         Create an XML report of coverage results.

        Use "{program_name} help <command>" for detailed help on any command.
    """,

    'minimum_help': """\
        Code coverage for Python, version {__version__} {extension_modifier}.  Use '{program_name} help' for help.
    """,

    'version': """\
        Coverage.py, version {__version__} {extension_modifier}
    """,
}


def main(argv=None):
    """The main entry point to coverage.py.

    This is installed as the script entry point.

    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        status = CoverageScript().command_line(argv)
    except ExceptionDuringRun as err:
        # An exception was caught while running the product code.  The
        # sys.exc_info() return tuple is packed into an ExceptionDuringRun
        # exception.
        traceback.print_exception(*err.args)    # pylint: disable=no-value-for-parameter
        status = ERR
    except BaseCoverageException as err:
        # A controlled error inside coverage.py: print the message to the user.
        msg = err.args[0]
        print(msg)
        status = ERR
    except SystemExit as err:
        # The user called `sys.exit()`.  Exit with their argument, if any.
        if err.args:
            status = err.args[0]
        else:
            status = None
    return status

# Profiling using ox_profile.  Install it from GitHub:
#   pip install git+https://github.com/emin63/ox_profile.git
#
# $set_env.py: COVERAGE_PROFILE - Set to use ox_profile.
_profile = os.environ.get("COVERAGE_PROFILE", "")
if _profile:                                                # pragma: debugging
    from ox_profile.core.launchers import SimpleLauncher    # pylint: disable=import-error
    original_main = main

    def main(argv=None):                                    # pylint: disable=function-redefined
        """A wrapper around main that profiles."""
        profiler = SimpleLauncher.launch()
        try:
            return original_main(argv)
        finally:
            data, _ = profiler.query(re_filter='coverage', max_records=100)
            print(profiler.show(query=data, limit=100, sep='', col=''))
            profiler.cancel()
