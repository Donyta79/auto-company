"""Resolve import paths only through the explicitly connected execution domain."""
import os
from pathlib import Path, PureWindowsPath
import subprocess

from center_store import CenterError


def resolve_import_path(value, domain, revision, adapter):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise CenterError('INVALID_INPUT', 'An explicit absolute source directory is required.')
    if not domain or domain.get('platform') != 'wsl':
        if not Path(value).is_absolute():
            raise CenterError('INVALID_PATH_FORMAT', 'Use an absolute host path, or connect the intended WSL environment first.')
        return {'root': str(Path(value).resolve()), 'executionRoot': value,
                'executionDomain': domain, 'environmentRevision': revision}
    if adapter.domain != domain:
        raise CenterError('EXECUTION_DOMAIN_CONFLICT', 'The connected environment changed; check the path again.', 409)
    windows = PureWindowsPath(value)
    if not value.startswith('/') and not windows.is_absolute():
        raise CenterError('INVALID_PATH_FORMAT', 'Use an absolute Windows or selected WSL path.')
    if windows.drive.lower().startswith(('\\\\wsl.localhost\\', '\\\\wsl$\\')):
        distribution = windows.drive.rsplit('\\', 1)[-1]
        if distribution.casefold() != domain['distribution'].casefold():
            raise CenterError('EXECUTION_DOMAIN_CONFLICT', 'The path belongs to another WSL distribution.', 409)
    prefix = ['wsl.exe', '--distribution', domain['distribution'], '--user', domain['user'], '--exec']

    def convert(flag, path):
        output = adapter._execute(prefix + ['wslpath', '-a', flag, path], timeout=15).stdout.strip()
        if not output or any(ord(c) < 32 for c in output):
            raise CenterError('INVALID_PATH_FORMAT', 'The selected environment could not map this path.')
        return output

    try:
        execution = value if value.startswith('/') else convert('-u', value)
        if not execution.startswith('/'):
            raise CenterError('INVALID_PATH_FORMAT', 'The selected environment returned a non-absolute path.')
        host = convert('-w', execution) if os.name == 'nt' else execution
        if os.name == 'nt' and not PureWindowsPath(host).is_absolute():
            raise CenterError('INVALID_PATH_FORMAT', 'The selected environment returned a non-absolute host path.')
        # Confirm the directory in that user/domain, never infer /mnt mappings.
        adapter._execute(prefix + ['test', '-d', execution], timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise CenterError('SOURCE_MISSING', 'The selected environment could not access or convert this directory.', 422) from error
    return {'root': host, 'executionRoot': execution,
            'executionDomain': dict(domain), 'environmentRevision': revision}
