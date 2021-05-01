# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Source file annotation for coverage.py."""

import io
import os
import re

from coverage.files import flat_rootname
from coverage.misc import ensure_dir, isolate_module
from coverage.report import get_analysis_to_report

os = isolate_module(os)


class AnnotateReporter(object):
    """Generate annotated source files showing line coverage.

    This reporter creates annotated copies of the measured source files. Each
    .py file is copied as a .py,cover file, with a left-hand margin annotating
    each line::

        > def h(x):
        -     if 0:   #pragma: no cover
        -         pass
        >     if x == 1:
        !         a = 1
        >     else:
        >         a = 2

        > h(2)

    Executed lines use '>', lines not executed use '!', lines excluded from
    consideration use '-'.

    """

    def __init__(self, coverage):
        self.coverage = coverage
        self.config = self.coverage.config
        self.directory = None

    blank_re = re.compile(r"\s*(#|$)")
    else_re = re.compile(r"\s*else\s*:\s*(#|$)")

    def report(self, morfs, directory=None):
        """Run the report.

        See `coverage.report()` for arguments.

        """
        self.directory = directory
        self.coverage.get_data()
        for fr, analysis in get_analysis_to_report(self.coverage, morfs):
            self.annotate_file(fr, analysis)

    def annotate_file(self, fr, analysis):
        """Annotate a single file.

        `fr` is the FileReporter for the file to annotate.

        """
        statements = sorted(analysis.statements)
        missing = sorted(analysis.missing)
        excluded = sorted(analysis.excluded)
        missing_branches = sorted(analysis.missing_branch_arcs().keys())

        if self.directory:
            ensure_dir(self.directory)
            dest_file = os.path.join(self.directory, flat_rootname(fr.relative_filename()))
            if dest_file.endswith("_py"):
                dest_file = dest_file[:-3] + ".py"
            dest_file += ",cover"
        else:
            dest_file = fr.filename + ",cover"

        with io.open(dest_file, 'w', encoding='utf8') as dest:
            source = fr.source()
            for lineno, line in enumerate(source.splitlines(True), start=1):
                # if lineno in analysis.statements:
                #     line_class.append("stm")
                if lineno in excluded:
                    dest.write(u'- ')
                elif lineno in missing:
                    dest.write(u'! ')
                elif lineno in missing_branches:
                    dest.write(u'~ ')
                elif lineno in statements:
                    dest.write(u'> ')
                else:
                    dest.write('  ')
                dest.write(line)
