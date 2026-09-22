#!/usr/bin/env python3
"""The pre-commit and CI check moved to src/threefold/tools/threefold_cli.py, because the stack packs it from there into /dist/threefold-bundle.zip.

This file runs that one in this module's own namespace, so
`python scripts/threefold_cli.py ...` works as it always did, and code that
loads this file by path sees every name the moved file defines. Its functions
read their globals from this module, so a name replaced here is replaced for
them too, which an import of the other module could not give.
"""
import os

_MOVED_TO = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "threefold", "tools", "threefold_cli.py")
)
with open(_MOVED_TO, "rb") as _handle:
    _code = compile(_handle.read(), _MOVED_TO, "exec", dont_inherit=True)
# Every path the moved file derives starts from __file__, which must name it
# rather than this shim. Its own `if __name__ == "__main__"` runs it as a script.
__file__ = _MOVED_TO
exec(_code, globals())
