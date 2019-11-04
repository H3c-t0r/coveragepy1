# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""TOML configuration support for coverage.py"""

import io
import os
import re

from coverage import env
from coverage.backward import configparser, path_types
from coverage.misc import CoverageException, substitute_variables


class TomlDecodeError(Exception):
    """An exception class that exists even when toml isn't installed."""
    pass


class TomlConfigParser:
    """TOML file reading with the interface of HandyConfigParser."""

    # This class has the same interface as config.HandyConfigParser, no
    # need for docstrings.
    # pylint: disable=missing-function-docstring

    def __init__(self, our_file):
        self.our_file = our_file
        self.getters = [lambda obj: obj['tool']['coverage']]
        if self.our_file:
            self.getters.append(lambda obj: obj)

        self._data = []

    def read(self, filenames):
        # RawConfigParser takes a filename or list of filenames, but we only
        # ever call this with a single filename.
        assert isinstance(filenames, path_types)
        filename = filenames
        if env.PYVERSION >= (3, 6):
            filename = os.fspath(filename)

        from coverage.optional import toml
        if toml is None:
            if self.our_file:
                raise CoverageException("Can't read {!r} without TOML support".format(filename))

        try:
            with io.open(filename, encoding='utf-8') as fp:
                toml_data = fp.read()
            toml_data = substitute_variables(toml_data, os.environ)
            if toml:
                try:
                    self._data.append(toml.loads(toml_data))
                except toml.TomlDecodeError as err:
                    raise TomlDecodeError(*err.args)
            elif re.search(r"^\[tool\.coverage\.", toml_data, flags=re.MULTILINE):
                # Looks like they meant to read TOML, but we can't.
                raise CoverageException("Can't read {!r} without TOML support".format(filename))
            else:
                return []
        except IOError:
            return []
        return [filename]

    def has_option(self, section, option):
        for data in self._data:
            for getter in self.getters:
                try:
                    getter(data)[section][option]
                except KeyError:
                    continue
                return True
        return False

    def has_section(self, section):
        for data in self._data:
            for getter in self.getters:
                try:
                    getter(data)[section]
                except KeyError:
                    continue
                return section
        return False

    def options(self, section):
        for data in self._data:
            for getter in self.getters:
                try:
                    section = getter(data)[section]
                except KeyError:
                    continue
                return list(section.keys())
        raise configparser.NoSectionError(section)

    def get_section(self, section):
        d = {}
        for opt in self.options(section):
            d[opt] = self.get(section, opt)
        return d

    def get(self, section, option):
        found_section = False
        for data in self._data:
            for getter in self.getters:
                try:
                    section = getter(data)[section]
                except KeyError:
                    continue

                found_section = True
                try:
                    value = section[option]
                except KeyError:
                    continue
                return value
        if not found_section:
            raise configparser.NoSectionError(section)
        raise configparser.NoOptionError(option, section)

    def getboolean(self, section, option):
        value = self.get(section, option)
        if not isinstance(value, bool):
            raise ValueError(
                'Option {!r} in section {!r} is not a boolean: {!r}'
                    .format(option, section, value)
            )
        return value

    def getlist(self, section, option):
        values = self.get(section, option)
        if not isinstance(values, list):
            raise ValueError(
                'Option {!r} in section {!r} is not a list: {!r}'
                    .format(option, section, values)
            )
        return values

    def getregexlist(self, section, option):
        values = self.getlist(section, option)
        for value in values:
            value = value.strip()
            try:
                re.compile(value)
            except re.error as e:
                raise CoverageException(
                    "Invalid [%s].%s value %r: %s" % (section, option, value, e)
                )
        return values

    def getint(self, section, option):
        value = self.get(section, option)
        if not isinstance(value, int):
            raise ValueError(
                'Option {!r} in section {!r} is not an integer: {!r}'
                    .format(option, section, value)
            )
        return value

    def getfloat(self, section, option):
        value = self.get(section, option)
        if isinstance(value, int):
            value = float(value)
        if not isinstance(value, float):
            raise ValueError(
                'Option {!r} in section {!r} is not a float: {!r}'
                    .format(option, section, value)
            )
        return value
