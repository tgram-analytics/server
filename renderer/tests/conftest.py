"""Make the renderer modules importable when running ``pytest renderer/tests``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
