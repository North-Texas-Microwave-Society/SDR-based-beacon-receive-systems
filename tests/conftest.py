import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


rtlsdr = types.ModuleType("rtlsdr")
rtlsdr.RtlSdr = object
sys.modules.setdefault("rtlsdr", rtlsdr)
