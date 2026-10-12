"""Make the web page an option of the build (the rest is in pyproject.toml).

`YAMUSIC_BACKEND_ONLY=1 pip install .` (or `python -m build`) leaves the
page (yamusic_radio/web, the build of frontend/) out of the package:
the radio then serves the stream and the API only.
"""

import os

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

BACKEND_ONLY = "YAMUSIC_BACKEND_ONLY"


class build_py(_build_py):  # noqa: N801
    """Leave the files of the page out when only the backend is wanted."""

    def find_data_files(self, package: str, src_dir: str) -> list[str]:
        files: list[str] = super().find_data_files(package, src_dir)
        if os.environ.get(BACKEND_ONLY, "").lower() not in ("", "0", "no"):
            files = [f for f in files if "/web/" not in f.replace(os.sep, "/")]
        return files


setup(cmdclass={"build_py": build_py})
