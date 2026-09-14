"""Select and check the Python backend before starting the application."""

import os
from pathlib import Path
import subprocess
import sys


CHECK_BACKEND = r"""
import inspect
from importlib.metadata import distribution
import os
from pathlib import Path
import re
import sys

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.common import PostProcessor
    from yt_dlp.version import __version__

    package = Path(yt_dlp.__file__).resolve()
    metadata = distribution('yt-dlp')
    name = re.sub(r'[-_.]+', '-', metadata.metadata.get('Name', '')).lower()
    if name != 'yt-dlp' or not metadata.metadata.get('Version', '').strip():
        raise RuntimeError('yt-dlp distribution metadata must contain a valid Name and Version')
    override = os.environ.get('YTDLP_OVERRIDE_DIR')
    if override is not None:
        root = Path(override).resolve()
        expected = root / 'yt_dlp'
        for source in (package, Path(inspect.getfile(YoutubeDL)).resolve(),
                       Path(inspect.getfile(PostProcessor)).resolve()):
            if not source.is_relative_to(expected):
                raise RuntimeError(f'backend import escaped the override: {source}')
        if Path(metadata.locate_file('')).resolve() != root:
            raise RuntimeError('override must include its own yt-dlp distribution metadata')
    mode = 'custom' if override is not None else 'stock'
    print(f'yt-dlp backend: {mode}, version {__version__}, {package}',
          file=sys.stderr, flush=True)
except Exception as error:
    print(f'Cannot load yt-dlp backend: {error}', file=sys.stderr, flush=True)
    sys.exit(1)
"""


def main():
    env = os.environ.copy()
    override = env.get("YTDLP_OVERRIDE_DIR")
    if override is not None:
        root = Path(override)
        if not override or not root.is_absolute():
            raise ValueError("YTDLP_OVERRIDE_DIR must be a nonempty absolute path")
        root = root.resolve(strict=True)
        if not (root / "yt_dlp" / "__init__.py").is_file():
            raise ValueError("YTDLP_OVERRIDE_DIR must contain a complete installed yt_dlp package")
        env["YTDLP_OVERRIDE_DIR"] = str(root)
        env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if (root / "bin").is_dir():
            env["PATH"] = str(root / "bin") + os.pathsep + env.get("PATH", os.defpath)

    # A fresh interpreter checks the same search path that the final process uses.
    check = subprocess.run([sys.executable, "-c", CHECK_BACKEND], env=env)
    if check.returncode:
        return check.returncode
    command = sys.argv[1:] or [sys.executable, str(Path(__file__).with_name("app.py"))]
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"Cannot start yt-dlp-web: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
