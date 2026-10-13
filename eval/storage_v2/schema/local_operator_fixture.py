"""Use the owned CI PostgreSQL container's Unix socket for local-only operators."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


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
            statement = Path(value).read_text() if option == '--file' else value
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
