import sys
from pathlib import Path

# Make topcon2csv.py importable from the tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
