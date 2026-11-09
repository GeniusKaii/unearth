"""Test package. Run with:  python -m unittest discover -s tests -t . -v"""

import os
import shutil
import uuid


def make_workdir(prefix: str = "t-") -> str:
    """A scratch dir inside the repo.

    Not the system temp dir: confined/sandboxed shells often cannot write there,
    and ``tempfile.mkdtemp`` leaves a mode-0700 ACL that some Windows sandboxes
    then refuse to let us write into.  Plain mkdir + chmod avoids both.
    """
    base = os.environ.get("UNEARTH_TEST_DIR") or os.path.join(os.getcwd(), ".testtmp")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, prefix + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return path


def clean(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
