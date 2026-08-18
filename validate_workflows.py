from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1])
paths = [root / '.github/workflows/rust-ci-full-nextest-platform.yml', root / '.github/workflows/rust-ci-full.yml']
for path in paths:
    yaml.safe_load(path.read_text())
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if 'taiki-e/install-action@' in line:
            window = '\n'.join(lines[i:i+6])
            if 'version:' in window:
                raise AssertionError(f'unsupported separate version input near {path}:{i+1}')
print(f'validated_workflows={len(paths)}')
