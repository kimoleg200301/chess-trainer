"""Install the pinned official Stockfish 19 release (GPLv3, sources included).

Used by Docker and CI so distribution package updates cannot change the engine.
HTTPS uses the normal system/proxy CA; archives are verified before extraction.
"""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

RELEASE = 'sf_19'
ASSETS = {
    'x86_64': ('x86-64', '9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f'),
    'aarch64': ('arm64', 'fe26cfd1d9db4c8af3d21e24d9ff34cacb31c1f940085a7583da11796f2bac01'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path('/opt/stockfish'))
    parser.add_argument('--link', type=Path, default=Path('/usr/games/stockfish'))
    parser.add_argument('--archive', type=Path, help='Use an already downloaded, checksum-verified archive')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ASSETS:
        parser.error('Supported platforms: Linux x86_64 and aarch64')
    arch, checksum = ASSETS[platform.machine()]
    name = f'stockfish-linux-{arch}-universal'
    url = f'https://github.com/official-stockfish/Stockfish/releases/download/{RELEASE}/{name}.tar.gz'
    with tempfile.TemporaryDirectory(prefix='stockfish-install-') as temp:
        archive = args.archive or Path(temp) / 'release.tar.gz'
        if not args.archive:
            with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        with archive.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != checksum:
                raise RuntimeError('Stockfish archive SHA-256 mismatch')
        extracted = Path(temp) / 'extracted'
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter='data')
        binary = extracted / 'stockfish' / name
        result = subprocess.run([binary], input='uci\nquit\n', capture_output=True,
                                text=True, check=True, timeout=15)
        if 'id name Stockfish 19' not in result.stdout.splitlines():
            raise RuntimeError('Downloaded engine did not identify as Stockfish 19')
        # Keep corresponding source, license and notices alongside the binary.
        # Never overwrite an existing installation silently.
        shutil.copytree(extracted / 'stockfish', args.destination)
    args.link.parent.mkdir(parents=True, exist_ok=True)
    args.link.symlink_to(args.destination.resolve() / name)
    print(f'Stockfish 19 installed: {args.link} (verified {checksum})')


if __name__ == '__main__':
    main()
