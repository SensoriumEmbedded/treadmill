"""The committed example files load as they stand, so a copy with real values
is the only step between a fresh clone and a configured station."""

import shutil
from pathlib import Path

from treadmill.station import load_station
from treadmill.station.config import repo_root

FIXTURES = Path(__file__).parent / "fixtures"


def test_example_rig_and_station_load(tmp_path: Path):
    examples = repo_root() / "data"
    (tmp_path / "rigs").mkdir()
    (tmp_path / "stations").mkdir()
    shutil.copy(examples / "rigs" / "rigA.example.toml", tmp_path / "rigs" / "rigA.toml")
    text = (examples / "stations" / "example.example.toml").read_text(encoding="utf-8")
    # The example points at registry id 29; the fixture registry has 1-5.
    (tmp_path / "stations" / "example.toml").write_text(
        text.replace('machine_id = "29"', 'machine_id = "1"'), encoding="utf-8")
    (tmp_path / "registry.toml").write_text(
        f'systems_csv = "{(FIXTURES / "systems.csv").as_posix()}"\n', encoding="utf-8")

    st = load_station("example", tmp_path)
    assert st.rig.id == "rigA" and st.rig.capture is not None
    assert st.rig.power is None and st.rig.region_switch is None and st.keyboard is None
    assert st.machine.nickname == "bench-a" and st.machine.boot_timeout_s == 60.0
    assert st.tr.uid == "00000000"
    assert st.settings_baseline == {"tcp_listener": False, "reset_detect": True, "autolaunch": "none",
                                    "show_extensions": False}
    assert st.has("video") and not st.has("power", "keys_hw", "eeprom_reset")

    # The commented [keyboard] table loads as written once its lines are uncommented.
    lines = []
    in_table = False
    for line in (tmp_path / "stations" / "example.toml").read_text(encoding="utf-8").splitlines():
        in_table = line.startswith("# [keyboard]") or (in_table and line.startswith("# ") and "=" in line)
        lines.append(line[2:] if in_table else line)
    (tmp_path / "stations" / "example.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    kb = load_station("example", tmp_path).keyboard
    assert (kb.chip, kb.kawari_open, kb.tod) == ("00000000", "NTSC", "standard")
