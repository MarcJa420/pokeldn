"""Builds the Android app: the local wheels, the native helpers (build_native.py, once), the staged
sources (stage.py), then the APK. Run with the repository's .venv Python from the repository root:

    ./.venv/bin/python examples/android/build.py [--release] [--install DEVICE]
"""
import argparse
import base64
import csv
import glob
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
WHEELS = HERE / 'wheels'
# Pure-Python wheels the Android build installs from files: sdists it cannot build, and msgpack
# without its C extension (build.gradle).
LOCAL = (('python-netlink==0.0.15', {}), ('esptool==5.4.0', {}), ('esp-pylib==1.1.5', {}),
         ('intelhex==2.3.0', {}), ('reedsolo==1.7.0', {}),
         ('msgpack==1.2.3', {'MSGPACK_PUREPYTHON': '1'}))


def prepare_esptool_wheel(path: Path) -> None:
    """Remove host-only console entry points and update RECORD for Chaquopy's Android install."""
    with ZipFile(path) as source:
        entries = {info.filename: (info, source.read(info.filename))
                   for info in source.infolist() if not info.is_dir()}

    entry_points = [name for name in entries if name.startswith('esptool-')
                    and name.endswith('.dist-info/entry_points.txt')]
    records = [name for name in entries if name.startswith('esptool-')
               and name.endswith('.dist-info/RECORD')]
    if len(entry_points) != 1 or len(records) != 1:
        raise ValueError(f'Unexpected esptool wheel metadata in {path}')

    # pip on Windows generates .exe launchers from these entry points. Chaquopy rejects
    # their ../../bin paths in RECORD, and Android calls esptool's Python API directly.
    entries[entry_points[0]] = (entries[entry_points[0]][0], b'')
    record_name = records[0]
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, (_, content) in entries.items():
        if name == record_name:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode('ascii')
        writer.writerow((name, f'sha256={digest}', len(content)))
    writer.writerow((record_name, '', ''))
    info, _ = entries[record_name]
    entries[record_name] = (info, record.getvalue().encode('utf-8'))

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.whl', delete=False) as file:
            temporary = Path(file.name)
        with ZipFile(temporary, 'w') as destination:
            for name, (info, content) in entries.items():
                destination.writestr(info, content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def gradle() -> str:
    """AGP 8.13 needs Gradle 8.13 to 9.5: GRADLE, else the newest 9.5 or older wrapper download, else PATH."""
    if os.environ.get('GRADLE'):
        return os.environ['GRADLE']
    found = sorted(glob.glob(str(Path.home() / '.gradle/wrapper/dists/gradle-*-bin/*/gradle-*/bin/gradle')))
    def version(path):
        return tuple(int(n) for n in Path(path).parents[1].name.removeprefix('gradle-').split('.') if n.isdigit())
    usable = sorted((g for g in found if (8, 13) <= version(g) < (9, 6)), key=version)
    return usable[-1] if usable else shutil.which('gradle') or 'gradle'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--release', action='store_true', help='assembleRelease instead of assembleDebug')
    parser.add_argument('--native', action='store_true', help='rebuild PKHeX and Unicorn even when present')
    parser.add_argument('--install', metavar='DEVICE', help='adb install the APK on this device')
    args = parser.parse_args()
    env = dict(os.environ)
    env.setdefault('ANDROID_HOME', str(Path.home() / 'Library/Android/sdk'))
    WHEELS.mkdir(exist_ok=True)
    for spec, extra in LOCAL:
        name = spec.split('==')[0].replace('-', '_')
        wheels = list(WHEELS.glob(f'{name}-*.whl'))
        if not wheels:
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-binary', ':all:',
                            '-w', str(WHEELS), spec], check=True, env={**env, **extra})
            wheels = list(WHEELS.glob(f'{name}-*.whl'))
        if name == 'esptool':
            if len(wheels) != 1:
                raise ValueError(f'Expected one esptool wheel in {WHEELS}, found {len(wheels)}')
            prepare_esptool_wheel(wheels[0])
    libs = HERE / 'jniLibs' / 'arm64-v8a'
    if args.native or not all((libs / n).is_file() for n in ('libpokeldn_pkhex.so', 'libunicorn.so')):
        subprocess.run([sys.executable, str(HERE / 'build_native.py')], check=True, env=env)
    subprocess.run([sys.executable, str(HERE / 'stage.py')], check=True, env=env)
    env.setdefault('POKELDN_ANDROID_BUILD_PYTHON', sys.executable)
    task = 'assembleRelease' if args.release else 'assembleDebug'
    subprocess.run([gradle(), '-p', str(HERE), '--no-daemon', '--console=plain', task], check=True, env=env)
    kind = 'release' if args.release else 'debug'
    apk = HERE / 'build' / 'outputs' / 'apk' / kind / f'pokeldn-{kind}.apk'
    print('APK:', apk)
    if args.install:
        subprocess.run(['adb', '-s', args.install, 'install', '-r', str(apk)], check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
