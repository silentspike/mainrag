"""Use the owned CI PostgreSQL container's Unix socket for local-only operators."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


def sql_file(path: Path, root: Path, parents=()) -> str:
    """Resolve psql includes on the host before sending fixture SQL over stdin."""
    path = path.resolve()
    if path in parents or len(parents) >= 8:
        raise AssertionError('Recursive fixture SQL include')
    def include(match):
        directory = path.parent if match.group(1) == 'ir' else root
        return sql_file(directory / match.group(2), root, (*parents, path))
    return re.sub(r'(?m)^\\(ir|i)\s+([^\s]+)[ \t]*$', include, path.read_text())


def command(cls, *arguments: str, check: bool = True):
    container = os.environ.get('STORAGE_V2_TEST_LOCAL_CONTAINER')
    if not container:
        return cls.base.command.__func__(cls, *arguments, check=check)
    if not re.fullmatch(r'[0-9a-f]{12,64}', container):
        raise AssertionError('Expected the exact owned PostgreSQL service container')
    arguments = list(arguments)
    statement = None
    for option in ('--command', '--file'):
        if option in arguments:
            index = arguments.index(option)
            value = arguments[index + 1]
            statement = sql_file(Path(value), cls.schema.ROOT) if option == '--file' else value
            del arguments[index:index + 2]
            break
    argv = ['docker', 'exec', '-i', container, 'psql', '-X', '--no-psqlrc', '-qAt',
            '-v', 'ON_ERROR_STOP=1', '--host=/var/run/postgresql',
            '--username', os.environ['PGUSER'], '--dbname', cls.database, *arguments]
    result = subprocess.run(argv, input=statement, text=True, capture_output=True,
                            cwd=cls.schema.ROOT)
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result
