from pathlib import Path
import sys


GROUP_DIR = Path(__file__).resolve().parent
ADMIN_DIR = GROUP_DIR.parent / '管理员'
sys.path.insert(0, str(ADMIN_DIR))

from experiment_core.cli import main


if __name__ == '__main__':
    raise SystemExit(main('hr', GROUP_DIR, ADMIN_DIR))
