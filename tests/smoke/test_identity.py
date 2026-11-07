"""Test 1: the board is the one the run says it is, running the build the
run flashed, in the machine the registry describes."""


def test_identity(at_menu, firmware, station):
    board = at_menu
    assert board.link.fw_check() == "main"
    banner = board.link.banner()
    assert banner.boot == "complete", banner.raw
    assert banner.uid == station.tr.uid, f"chip id {banner.uid} is not the station's {station.tr.uid}"
    if firmware.spec.target != "auto":
        assert banner.kind == firmware.spec.target, banner.version
    if firmware.hex is not None:
        assert banner.version == firmware.hex.version, f"{banner.version!r} on the board, {firmware.hex.version!r} flashed"
        assert banner.built == firmware.hex.stamp, f"{banner.built!r} on the board, {firmware.hex.stamp!r} flashed"
        if firmware.hex.kind:
            assert banner.kind == firmware.hex.kind
    expected = "C128" if station.machine.is_c128 else "C64"
    assert banner.machine == expected, f"the board sees a {banner.machine}; the registry says {station.machine.model}"
    region = station.machine.region.upper()
    if region in ("PAL", "NTSC"):
        assert banner.video == region, f"the board sees {banner.video}; the registry says {region}"
    assert banner.hz in (50, 60), banner.raw
