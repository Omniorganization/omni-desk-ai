#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version_from_wheel(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith('.dist-info/METADATA'):
                text = zf.read(name).decode('utf-8', errors='replace')
                for line in text.splitlines():
                    if line.startswith('Version: '):
                        return line.split(':', 1)[1].strip()
    raise RuntimeError(f'could not read version from {path}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Write OmniDesk release metadata for artifact/image/runtime binding.')
    parser.add_argument('dist_dir', nargs='?', default='dist')
    parser.add_argument('--build-sha', default=os.getenv('GITHUB_SHA') or os.getenv('OMNIDESK_BUILD_SHA') or 'unknown')
    parser.add_argument('--image-digest', default=os.getenv('OMNIDESK_IMAGE_DIGEST') or '')
    parser.add_argument('--image-ref', default=os.getenv('OMNIDESK_IMAGE_REF') or '')
    parser.add_argument('--web-admin-image-digest', default=os.getenv('OMNIDESK_WEB_ADMIN_IMAGE_DIGEST') or '')
    parser.add_argument('--web-admin-image-ref', default=os.getenv('OMNIDESK_WEB_ADMIN_IMAGE_REF') or '')
    parser.add_argument('--output', default='release_metadata.json')
    args = parser.parse_args(argv)

    dist = Path(args.dist_dir)
    wheels = sorted(dist.glob('*.whl'))
    if not wheels:
        print('no wheel artifact found', file=sys.stderr)
        return 1
    wheel = wheels[0]
    version = _version_from_wheel(wheel)
    artifact_sha256 = _sha256(wheel)
    sbom_path = dist / 'sbom.json'
    metadata = {
        'schema_version': 2,
        'package': 'omnidesk-agent',
        'version': version,
        'build_sha': args.build_sha,
        'artifact': {
            'name': wheel.name,
            'sha256': artifact_sha256,
        },
        'sbom': {
            'name': sbom_path.name if sbom_path.exists() else '',
            'sha256': _sha256(sbom_path) if sbom_path.exists() else '',
        },
        'image': {
            'ref': args.image_ref,
            'digest': args.image_digest,
        },
        'web_admin_image': {
            'ref': args.web_admin_image_ref,
            'digest': args.web_admin_image_digest,
        },
        'integrity': {
            'checksums_manifest': 'checksums.txt',
            'standard_checksums_manifest': 'SHA256SUMS.txt',
            'signature_manifest': 'release_signatures.json',
            'note': 'checksums.txt and SHA256SUMS.txt cover the same release payload set; release_signatures.json signs both checksum manifests and all release artifacts.',
        },
    }
    output = dist / args.output
    output.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'wrote {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
