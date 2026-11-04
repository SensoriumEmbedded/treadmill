"""Rig and station files load, the registry maps, capabilities derive."""

from pathlib import Path

import pytest

from treadmill.station import ConfigError, list_stations, load_rig, load_station
from treadmill.station.model import TR_PLUS, Station, capabilities, tier, unknown_caps
from treadmill.station.registry import normalize_model, normalize_region, read_registry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    """A data directory with one rig, one station and the fixture registry."""
    (tmp_path / "rigs").mkdir()
    (tmp_path / "stations").mkdir()
    (tmp_path / "rigs" / "rigA.toml").write_text(
        'id = "rigA"\nregion_switch = "kawari"\nscope = true\n'
        '[capture]\nbackend = "opencv"\ndevice = 0\nsource = "svideo"\nconverter = "acme"\n'
        '[power]\nbackend = "none"\n'
        '[keys]\nbackend = "usbkeyboard"\nport = "COM8"\n', encoding="utf-8")
    (tmp_path / "stations" / "rigA-sys1-trplus.toml").write_text(
        'id = "rigA-sys1-trplus"\nrig = "rigA"\nmachine_id = "1"\n'
        '[overrides]\nkernal = "JiffyDOS"\nregion = "switchable"\n'
        '[tr]\nuid = "12345678"\nport_hint = "COM91"\n', encoding="utf-8")
    (tmp_path / "registry.toml").write_text(
        f'systems_csv = "{(FIXTURES / "systems.csv").as_posix()}"\n', encoding="utf-8")
    return tmp_path


def test_registry_reads_and_normalizes():
    machines = read_registry(FIXTURES / "systems.csv")
    assert set(machines) == {"1", "2", "3", "4", "5"}
    assert machines["1"].has_kawari and machines["1"].model == "C64"
    assert machines["2"].region == "PAL"
    assert machines["3"].is_c128
    assert machines["4"].is_c64u and machines["4"].timing_tolerant
    assert machines["5"].model == "SX-64" and machines["5"].nickname == ""


@pytest.mark.parametrize("text,model", [("C64", "C64"), ("c64c", "C64"), ("C128D", "C128"),
                                        ("Ultimate 64", "C64U"), ("SX-64", "SX-64")])
def test_model_normalization(text, model):
    assert normalize_model(text) == model


def test_region_normalization():
    assert normalize_region("pal") == "PAL"
    assert normalize_region("NTSC") == "NTSC"
    assert normalize_region("switchable") == "switchable"


def test_station_loads_with_registry_and_overrides(lab: Path):
    st = load_station("rigA-sys1-trplus", lab)
    assert st.rig.capture.capture_class == "svideo-acme"
    assert st.machine.nickname == "bench-a" and st.machine.kernal == "JiffyDOS"
    assert st.tr.uid == "12345678" and st.tr.kind is None
    # Before the probe: video, keys from the board, region switch, scope; no DMA yet.
    assert st.has("serial", "video", "screen", "keys_hw", "keys_kernal", "region_switch", "scope")
    assert not st.has("dma")
    assert "kernal:JiffyDOS" in st.caps
    assert tier(st) == "0/1/3"


def test_probe_adds_dma_caps_and_mask_removes_them(lab: Path):
    st = load_station("rigA-sys1-trplus", lab).with_probe(TR_PLUS, fw_defines=("Dbg_SerDMA",))
    assert st.has("dma", "screen_dma", "reset_sense", "fw:Dbg_SerDMA")
    assert tier(st) == "0+/1/3"
    masked = st.masked({"dma", "screen_dma", "reset_sense", "keys_kernal", "keys_hw"})
    assert not masked.has("dma") and masked.has("video")
    assert tier(masked) == "0/3"


def test_list_stations_skips_examples(lab: Path):
    (lab / "stations" / "x.example.toml").write_text("", encoding="utf-8")
    assert list_stations(lab) == ["rigA-sys1-trplus"]


def test_unknown_keys_and_missing_pieces_fail_loudly(lab: Path):
    (lab / "rigs" / "bad.toml").write_text('id = "bad"\n[capture]\nbackend = "opencv"\nfourc = "MJPG"\n',
                                           encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_rig("bad", lab)
    (lab / "stations" / "nomachine.toml").write_text('id = "nomachine"\nrig = "rigA"\n[tr]\nuid = "1"\n',
                                                     encoding="utf-8")
    with pytest.raises(ConfigError, match="machine"):
        load_station("nomachine", lab)
    with pytest.raises(ConfigError, match="does not exist"):
        load_station("absent", lab)


def test_unknown_capability_names_are_reported():
    assert unknown_caps(("dma", "kernal:stock", "fw:Dbg_SerDMA")) == []
    assert unknown_caps(("dmx", "video")) == ["dmx"]


def test_inline_machine_without_registry(tmp_path: Path):
    (tmp_path / "rigs").mkdir()
    (tmp_path / "stations").mkdir()
    (tmp_path / "rigs" / "r.toml").write_text('id = "r"\n', encoding="utf-8")
    (tmp_path / "stations" / "s.toml").write_text(
        'id = "s"\nrig = "r"\n[machine]\nid = "bench"\nmodel = "C128"\nregion = "NTSC"\n'
        '[tr]\nuid = "9"\n', encoding="utf-8")
    st = load_station("s", tmp_path)
    assert isinstance(st, Station) and st.has("c128") and not st.has("video")
    assert capabilities(st) == st.caps
