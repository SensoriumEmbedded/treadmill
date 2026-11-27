"""Rig and station files load, the registry maps, capabilities derive."""

from pathlib import Path

import pytest

from treadmill.margins import Floor
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
        'id = "rigA"\nscope = true\n'
        '[capture]\nbackend = "opencv"\ndevice = 0\nsource = "svideo"\nconverter = "acme"\n'
        '[power]\nbackend = "none"\n'
        '[keys]\nbackend = "none"\n', encoding="utf-8")
    (tmp_path / "stations" / "rigA-sys1-trplus.toml").write_text(
        'id = "rigA-sys1-trplus"\nrig = "rigA"\nmachine_id = "1"\n'
        '[overrides]\nkernal = "JiffyDOS"\n'
        '[tr]\nuid = "12345678"\nport_hint = "COM91"\n'
        '[keyboard]\nchip = 14470230\nkawari_open = "ntsc"\n', encoding="utf-8")
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
    assert st.keyboard.chip == "14470230" and st.keyboard.kawari_open == "NTSC" and st.keyboard.tod == "standard"
    # Before the probe and before the keyboard board answers: video and scope; no DMA, no keys yet.
    assert st.has("serial", "video", "screen", "scope")
    assert not st.has("dma") and not st.has("keys_hw") and st.kbd_fitted is None
    assert "kernal:JiffyDOS" in st.caps
    assert tier(st) == "0"
    st = st.with_keyboard(frozenset({"tod", "reset", "kawari"}))
    assert st.has("keys_hw", "keys_kernal", "nmi", "tod_select", "ext_reset", "region_switch")
    assert not st.has("tr_button") and not st.has("tr_alt")
    assert tier(st) == "0/1/3"


def test_a_station_reads_with_its_own_bank_before_its_capture_class_s(lab: Path):
    from treadmill.session import bank_path, find_bank

    st = load_station("rigA-sys1-trplus", lab)
    own, shared = lab / "glyphs" / "svideo-acme__1.npz", lab / "glyphs" / "svideo-acme.npz"
    assert bank_path(st, lab) == own and find_bank(st, lab) == own        # nothing harvested: where one goes
    shared.parent.mkdir()
    shared.write_bytes(b"")
    assert find_bank(st, lab) == shared
    own.write_bytes(b"")
    assert find_bank(st, lab) == own


def test_probe_adds_dma_caps_and_mask_removes_them(lab: Path):
    st = load_station("rigA-sys1-trplus", lab).with_keyboard(frozenset({"reset", "kawari"}))
    st = st.with_probe(TR_PLUS, fw_defines=("Dbg_SerDMA",))
    assert st.has("dma", "screen_dma", "reset_sense", "fw:Dbg_SerDMA", "keys_hw", "region_switch")
    assert tier(st) == "0+/1/3"
    masked = st.masked({"dma", "screen_dma", "reset_sense", "keys_kernal", "keys_hw"})
    assert not masked.has("dma") and masked.has("video")
    assert tier(masked) == "0/3"


def test_the_keyboard_board_keeps_the_probe_s_caps(lab: Path):
    st = load_station("rigA-sys1-trplus", lab).with_probe(TR_PLUS, fw_defines=("Dbg_SerDMA",))
    st = st.with_keyboard(frozenset())
    assert st.has("dma", "fw:Dbg_SerDMA", "keys_hw", "keys_kernal", "nmi")
    assert not st.has("ext_reset") and not st.has("region_switch") and not st.has("tod_select")


@pytest.mark.parametrize("fitted, has_kawari, kawari_open, granted, refused", [
    (set(), True, "NTSC", {"keys_hw", "keys_kernal", "nmi"},
     {"tod_select", "tr_button", "tr_alt", "ext_reset", "region_switch"}),
    ({"tod"}, True, "NTSC", {"tod_select"}, {"ext_reset"}),
    ({"menu"}, True, "NTSC", {"tr_button"}, {"tr_alt"}),
    ({"alt"}, True, "NTSC", {"tr_alt"}, {"tr_button"}),
    ({"reset"}, True, "NTSC", {"ext_reset"}, {"region_switch"}),
    ({"kawari"}, True, "NTSC", set(), {"region_switch"}),                 # V holds reset: it needs both
    ({"kawari", "reset"}, False, "NTSC", {"ext_reset"}, {"region_switch"}),  # no Kawari in this machine
    ({"kawari", "reset"}, True, None, {"ext_reset"}, {"region_switch"}),     # J1 open's standard unknown
    ({"kawari", "reset"}, True, "PAL", {"ext_reset", "region_switch"}, set()),
])
def test_keyboard_caps_follow_what_is_fitted(lab: Path, fitted, has_kawari, kawari_open, granted, refused):
    from dataclasses import replace

    st = load_station("rigA-sys1-trplus", lab)
    st = replace(st, machine=replace(st.machine, has_kawari=has_kawari),
                 keyboard=replace(st.keyboard, kawari_open=kawari_open)).with_keyboard(frozenset(fitted))
    assert granted <= st.caps and not (refused & st.caps)


@pytest.mark.parametrize("rig, match", [
    ('id = "r"\nregion_switch = "kawari"\n', "keyboard board switches a Kawari"),
    ('id = "r"\nregion_switch = "relay"\n', "the one value"),
    ('id = "r"\n[keys]\nbackend = "usbkeyboard"\nport = "COM8"\n', "station file now"),
])
def test_old_rig_keyboard_settings_say_where_they_went(tmp_path: Path, rig, match):
    (tmp_path / "rigs").mkdir()
    (tmp_path / "rigs" / "r.toml").write_text(rig, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_rig("r", tmp_path)


def test_a_rig_names_its_mains_relay_by_unit_and_channel(tmp_path: Path):
    (tmp_path / "rigs").mkdir()
    (tmp_path / "rigs" / "r.toml").write_text(
        'id = "r"\n[power]\nbackend = "serial-relay"\nunit = "12345678"\noff_seconds = 8.0\n', encoding="utf-8")
    power = load_rig("r", tmp_path).power
    assert (power.backend, power.unit, power.channel, power.off_seconds) == ("serial-relay", "12345678", 1, 8.0)


def test_the_old_power_port_says_what_to_write_instead(tmp_path: Path):
    (tmp_path / "rigs").mkdir()
    (tmp_path / "rigs" / "r.toml").write_text('id = "r"\n[power]\nbackend = "serial-relay"\nport = "COM7"\n',
                                             encoding="utf-8")
    with pytest.raises(ConfigError, match="port is gone.*unit ="):
        load_rig("r", tmp_path)


@pytest.mark.parametrize("table, match", [
    ('kawari_open = "PAL"\n', "needs chip"),
    ('chip = "C64USBKeyboard_1"\n', "not a chip id"),
    ('chip = "1"\nkawari_open = "SECAM"\n', "PAL or NTSC"),
    ('chip = "1"\ntod = 55\n', "one of standard, 50, 60, off"),
    ('chip = "1"\nport = "COM5"\n', "unknown key"),
])
def test_a_bad_keyboard_table_fails_loudly(lab: Path, table, match):
    (lab / "stations" / "kb.toml").write_text(
        f'id = "kb"\nrig = "rigA"\nmachine_id = "1"\n[tr]\nuid = "1"\n[keyboard]\n{table}', encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_station("kb", lab)


def test_floors_are_per_direction(lab: Path):
    (lab / "stations" / "fl.toml").write_text(
        'id = "fl"\nrig = "rigA"\nmachine_id = "1"\n[tr]\nuid = "1"\n'
        '[margins]\nnS_PLAprop = { up = 200, down = 140 }\nth = { down = 330 }\n', encoding="utf-8")
    assert load_station("fl", lab).margins == {"nS_PLAprop": Floor(up=200, down=140), "nS_DataHold": Floor(down=330)}


@pytest.mark.parametrize("table, match", [
    ("nS_PLAprop = 200", "a floor per direction"),
    ("nS_PLAprop = { left = 1 }", "only up and down"),
    ("nS_PLAplop = { up = 1 }", "no timing knob"),
])
def test_a_bad_floor_fails_loudly(lab: Path, table, match):
    (lab / "stations" / "fl.toml").write_text(
        f'id = "fl"\nrig = "rigA"\nmachine_id = "1"\n[tr]\nuid = "1"\n[margins]\n{table}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_station("fl", lab)


def test_the_port_setup_reaches_the_run_and_every_margin_record(lab: Path):
    from types import SimpleNamespace
    from treadmill.margins import context_for
    from treadmill.report import describe_station
    assert load_station("rigA-sys1-trplus", lab).port_setup is None
    (lab / "stations" / "ps.toml").write_text(
        'id = "ps"\nrig = "rigA"\nmachine_id = "1"\nport_setup = " X-Pander 3, slot 1 "\n[tr]\nuid = "1"\n',
        encoding="utf-8")
    st = load_station("ps", lab)
    assert st.port_setup == "X-Pander 3, slot 1"
    board = SimpleNamespace(station=st, banner=None, kbd=None, kbd_problem=None)
    assert describe_station(board)["port_setup"] == "X-Pander 3, slot 1"
    assert context_for(board)["port_setup"] == "X-Pander 3, slot 1"


@pytest.mark.parametrize("value", ["1", '""', "true"])
def test_a_port_setup_that_is_not_text_fails_loudly(lab: Path, value):
    (lab / "stations" / "ps.toml").write_text(
        f'id = "ps"\nrig = "rigA"\nmachine_id = "1"\nport_setup = {value}\n[tr]\nuid = "1"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="port_setup is text"):
        load_station("ps", lab)


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
