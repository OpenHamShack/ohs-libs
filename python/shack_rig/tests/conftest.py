import sys
from pathlib import Path

# helpers.py sits beside the tests rather than in the package: it is test
# scaffolding, and an app that wants a fake radio uses shack_rig.icom.fake.
sys.path.insert(0, str(Path(__file__).resolve().parent))
