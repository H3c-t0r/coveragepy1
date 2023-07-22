# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Results of coverage measurement."""

from __future__ import annotations

import collections

from typing import Callable, Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING

from coverage.debug import auto_repr
from coverage.exceptions import ConfigError
from coverage.misc import nice_pair
from coverage.types import TArc, TLineNo

if TYPE_CHECKING:
    from coverage.data import CoverageData
    from coverage.plugin import FileReporter


class Analysis:
    """The results of analyzing a FileReporter."""

    def __init__(
        self,
        data: CoverageData,
        precision: int,
        file_reporter: FileReporter,
        file_mapper: Callable[[str], str],
    ) -> None:
        self.data = data
        self.file_reporter = file_reporter
        self.filename = file_mapper(self.file_reporter.filename)
        self.statements = self.file_reporter.lines()
        self.excluded = self.file_reporter.excluded_lines()

        # Identify missing statements.
        executed: Iterable[TLineNo]
        executed = self.data.lines(self.filename) or []
        executed = self.file_reporter.translate_lines(executed)
        self.executed = executed
        self.missing = self.statements - self.executed

        if self.data.has_arcs():
            self._arc_possibilities = sorted(self.file_reporter.arcs())
            self.exit_counts = self.file_reporter.exit_counts()
            self.no_branch = self.file_reporter.no_branch_lines()
            n_branches = self._total_branches()
            mba = self.missing_branch_arcs()
            n_partial_branches = sum(len(v) for k,v in mba.items() if k not in self.missing)
            n_missing_branches = sum(len(v) for k,v in mba.items())
        else:
            self._arc_possibilities = []
            self.exit_counts = {}
            self.no_branch = set()
            n_branches = n_partial_branches = n_missing_branches = 0

        self.numbers = Numbers(
            precision=precision,
            n_files=1,
            n_statements=len(self.statements),
            n_excluded=len(self.excluded),
            n_missing=len(self.missing),
            n_branches=n_branches,
            n_partial_branches=n_partial_branches,
            n_missing_branches=n_missing_branches,
        )

    def missing_formatted(self, branches: bool = False) -> str:
        """The missing line numbers, formatted nicely.

        Returns a string like "1-2, 5-11, 13-14".

        If `branches` is true, includes the missing branch arcs also.

        """
        if branches and self.has_arcs():
            arcs = self.missing_branch_arcs().items()
        else:
            arcs = None

        return format_lines(self.statements, self.missing, arcs=arcs)

    def has_arcs(self) -> bool:
        """Were arcs measured in this result?"""
        return self.data.has_arcs()

    def arc_possibilities(self) -> List[TArc]:
        """Returns a sorted list of the arcs in the code."""
        return self._arc_possibilities

    def arcs_executed(self) -> List[TArc]:
        """Returns a sorted list of the arcs actually executed in the code."""
        executed: Iterable[TArc]
        executed = self.data.arcs(self.filename) or []
        executed = self.file_reporter.translate_arcs(executed)
        return sorted(executed)

    def arcs_missing(self) -> List[TArc]:
        """Returns a sorted list of the un-executed arcs in the code."""
        possible = self.arc_possibilities()
        executed = self.arcs_executed()
        missing = (
            p for p in possible
                if p not in executed
                    and p[0] not in self.no_branch
                    and p[1] not in self.excluded
        )
        return sorted(missing)

    def arcs_unpredicted(self) -> List[TArc]:
        """Returns a sorted list of the executed arcs missing from the code."""
        possible = self.arc_possibilities()
        executed = self.arcs_executed()
        # Exclude arcs here which connect a line to itself.  They can occur
        # in executed data in some cases.  This is where they can cause
        # trouble, and here is where it's the least burden to remove them.
        # Also, generators can somehow cause arcs from "enter" to "exit", so
        # make sure we have at least one positive value.
        unpredicted = (
            e for e in executed
                if e not in possible
                    and e[0] != e[1]
                    and (e[0] > 0 or e[1] > 0)
        )
        return sorted(unpredicted)

    def _branch_lines(self) -> List[TLineNo]:
        """Returns a list of line numbers that have more than one exit."""
        return [l1 for l1,count in self.exit_counts.items() if count > 1]

    def _total_branches(self) -> int:
        """How many total branches are there?"""
        return sum(count for count in self.exit_counts.values() if count > 1)

    def missing_branch_arcs(self) -> Dict[TLineNo, List[TLineNo]]:
        """Return arcs that weren't executed from branch lines.

        Returns {l1:[l2a,l2b,...], ...}

        """
        missing = self.arcs_missing()
        branch_lines = set(self._branch_lines())
        mba = collections.defaultdict(list)
        for l1, l2 in missing:
            if l1 in branch_lines:
                mba[l1].append(l2)
        return mba

    def executed_branch_arcs(self) -> Dict[TLineNo, List[TLineNo]]:
        """Return arcs that were executed from branch lines.

        Returns {l1:[l2a,l2b,...], ...}

        """
        executed = self.arcs_executed()
        branch_lines = set(self._branch_lines())
        eba = collections.defaultdict(list)
        for l1, l2 in executed:
            if l1 in branch_lines:
                eba[l1].append(l2)
        return eba

    def branch_stats(self) -> Dict[TLineNo, Tuple[int, int]]:
        """Get stats about branches.

        Returns a dict mapping line numbers to a tuple:
        (total_exits, taken_exits).
        """

        missing_arcs = self.missing_branch_arcs()
        stats = {}
        for lnum in self._branch_lines():
            exits = self.exit_counts[lnum]
            missing = len(missing_arcs[lnum])
            stats[lnum] = (exits, exits - missing)
        return stats


class Numbers:
    """The numerical results of measuring coverage.

    This holds the basic statistics from `Analysis`, and is used to roll
    up statistics across files.

    """

    def __init__(
        self,
        precision: int = 0,
        n_files: int = 0,
        n_statements: int = 0,
        n_excluded: int = 0,
        n_missing: int = 0,
        n_branches: int = 0,
        n_partial_branches: int = 0,
        n_missing_branches: int = 0,
    ) -> None:
        assert 0 <= precision < 10
        self._precision = precision
        self._near0 = 1.0 / 10**precision
        self._near100 = 100.0 - self._near0
        self.n_files = n_files
        self.n_statements = n_statements
        self.n_excluded = n_excluded
        self.n_missing = n_missing
        self.n_branches = n_branches
        self.n_partial_branches = n_partial_branches
        self.n_missing_branches = n_missing_branches

    __repr__ = auto_repr

    def init_args(self) -> List[int]:
        """Return a list for __init__(*args) to recreate this object."""
        return [
            self._precision,
            self.n_files, self.n_statements, self.n_excluded, self.n_missing,
            self.n_branches, self.n_partial_branches, self.n_missing_branches,
        ]

    @property
    def n_executed(self) -> int:
        """Returns the number of executed statements."""
        return self.n_statements - self.n_missing

    @property
    def n_executed_branches(self) -> int:
        """Returns the number of executed branches."""
        return self.n_branches - self.n_missing_branches

    @property
    def pc_covered(self) -> float:
        """Returns a single percentage value for coverage."""
        if self.n_statements > 0:
            numerator, denominator = self.ratio_covered
            pc_cov = (100.0 * numerator) / denominator
        else:
            pc_cov = 100.0
        return pc_cov

    @property
    def pc_covered_str(self) -> str:
        """Returns the percent covered, as a string, without a percent sign.

        Note that "0" is only returned when the value is truly zero, and "100"
        is only returned when the value is truly 100.  Rounding can never
        result in either "0" or "100".

        """
        return self.display_covered(self.pc_covered)

    def display_covered(self, pc: float) -> str:
        """Return a displayable total percentage, as a string.

        Note that "0" is only returned when the value is truly zero, and "100"
        is only returned when the value is truly 100.  Rounding can never
        result in either "0" or "100".

        """
        if 0 < pc < self._near0:
            pc = self._near0
        elif self._near100 < pc < 100:
            pc = self._near100
        else:
            pc = round(pc, self._precision)
        return "%.*f" % (self._precision, pc)

    def pc_str_width(self) -> int:
        """How many characters wide can pc_covered_str be?"""
        width = 3   # "100"
        if self._precision > 0:
            width += 1 + self._precision
        return width

    @property
    def ratio_covered(self) -> Tuple[int, int]:
        """Return a numerator and denominator for the coverage ratio."""
        numerator = self.n_executed + self.n_executed_branches
        denominator = self.n_statements + self.n_branches
        return numerator, denominator

    def __add__(self, other: Numbers) -> Numbers:
        nums = Numbers(precision=self._precision)
        nums.n_files = self.n_files + other.n_files
        nums.n_statements = self.n_statements + other.n_statements
        nums.n_excluded = self.n_excluded + other.n_excluded
        nums.n_missing = self.n_missing + other.n_missing
        nums.n_branches = self.n_branches + other.n_branches
        nums.n_partial_branches = (
            self.n_partial_branches + other.n_partial_branches
        )
        nums.n_missing_branches = (
            self.n_missing_branches + other.n_missing_branches
        )
        return nums

    def __radd__(self, other: int) -> Numbers:
        # Implementing 0+Numbers allows us to sum() a list of Numbers.
        assert other == 0   # we only ever call it this way.
        return self


def _line_ranges(
    statements: Iterable[TLineNo],
    lines: Iterable[TLineNo],
) -> List[Tuple[TLineNo, TLineNo]]:
    """Produce a list of ranges for `format_lines`."""
    statements = sorted(statements)
    lines = sorted(lines)

    pairs = []
    start: Optional[TLineNo] = None
    lidx = 0
    for stmt in statements:
        if lidx >= len(lines):
            break
        if stmt == lines[lidx]:
            lidx += 1
            if not start:
                start = stmt
            end = stmt
        elif start:
            pairs.append((start, end))
            start = None
    if start:
        pairs.append((start, end))
    return pairs


def format_lines(
    statements: Iterable[TLineNo],
    lines: Iterable[TLineNo],
    arcs: Optional[Iterable[Tuple[TLineNo, List[TLineNo]]]] = None,
) -> str:
    """Nicely format a list of line numbers.

    Format a list of line numbers for printing by coalescing groups of lines as
    long as the lines represent consecutive statements.  This will coalesce
    even if there are gaps between statements.

    For example, if `statements` is [1,2,3,4,5,10,11,12,13,14] and
    `lines` is [1,2,5,10,11,13,14] then the result will be "1-2, 5-11, 13-14".

    Both `lines` and `statements` can be any iterable. All of the elements of
    `lines` must be in `statements`, and all of the values must be positive
    integers.

    If `arcs` is provided, they are (start,[end,end,end]) pairs that will be
    included in the output as long as start isn't in `lines`.

    """
    line_items = [(pair[0], nice_pair(pair)) for pair in _line_ranges(statements, lines)]
    if arcs is not None:
        line_exits = sorted(arcs)
        for line, exits in line_exits:
            for ex in sorted(exits):
                if line not in lines and ex not in lines:
                    dest = (ex if ex > 0 else "exit")
                    line_items.append((line, f"{line}->{dest}"))

    ret = ", ".join(t[-1] for t in sorted(line_items))
    return ret


def should_fail_under(total: float, fail_under: float, precision: int) -> bool:
    """Determine if a total should fail due to fail-under.

    `total` is a float, the coverage measurement total. `fail_under` is the
    fail_under setting to compare with. `precision` is the number of digits
    to consider after the decimal point.

    Returns True if the total should fail.

    """
    # We can never achieve higher than 100% coverage, or less than zero.
    if not (0 <= fail_under <= 100.0):
        msg = f"fail_under={fail_under} is invalid. Must be between 0 and 100."
        raise ConfigError(msg)

    # Special case for fail_under=100, it must really be 100.
    if fail_under == 100.0 and total != 100.0:
        return True

    return round(total, precision) < fail_under
