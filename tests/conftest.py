import os
import sys
import tempfile
from pathlib import Path

# The service writes its data and VAPID key next to GECKO_DATA on import, so point it at a throwaway folder.
os.environ.setdefault("GECKO_DATA", str(Path(tempfile.mkdtemp(prefix="gecko-test-")) / "data"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
