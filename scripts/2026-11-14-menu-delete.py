"""2026-11-14: the menu's INST/DEL file delete, the cases not yet tried on hardware.

    treadmill exec --station <id> --fw ref=main scripts/2026-11-14-menu-delete.py --json

Firmware: main at or after "menu: INST/DEL deletes the highlighted file on SD or USB".
Needs keys (a TR+ station) and an SD card. What runs depends on what the station has:

  only_file_subfolder  SD: a folder's only file is deleted; the listing is the up-dir row alone,
                       with the cursor on it. (<Empty> is a drive root's row, never a folder's.)
  only_file_root       USB, when a drive is attached and empty: its only file is deleted and the
                       listing is <Empty> with the cursor on that row.
  del_on_empty         INST/DEL on <Empty> (an empty or absent USB drive) is refused.
  vms_claim            a /VMS package's manifest.vmi is deleted from the menu; a file of a type
                       only that package claimed then lists as Unk. The manifest is read first
                       and posted back whatever happens in between.

Everything the script puts on a drive it removes again, except the empty folder
/treadmill/deltest on the card (the protocol has no rmdir).
"""
import sys
import time
import traceback

from treadmill.c64.keys import CRSR_RIGHT, DELETE, SPACE
from treadmill.c64.menu import EMPTY, FIRST_ITEM_ROW, ITEMS_PER_PAGE, MARK_UNKNOWN, UP_DIR, MenuError
from treadmill.c64.screen import SCREEN_COLUMNS
from treadmill.link.trlink import LinkError

KEY_Y = 0x59                        # what the menu's cmp #'y' compares with under !convtab pet
ROOT, FOLDER = "/treadmill", "deltest"
DIR = f"{ROOT}/{FOLDER}"
ONLY = "ONLY.DEL"                   # an unknown type, so every listing shows the whole name
PROMPT = "Delete this file? y/n"
ANY_KEY = "Press any key to return"
REFUSED_NOT_A_FILE = "Only files can be deleted"

SELECTED = set(sys.argv[1:])        # case names after --; none means all of them

cases: dict[str, dict] = {}


def same(a: str, b: str) -> bool:
    """Names compared as the menu lists them: the firmware lowercases a file's extension in
    place when it works out the type (Assoc_Ext_ItemType), so ONLY.DEL lists as ONLY.del."""
    return a.lower() == b.lower()


def save(tag: str, read):
    (artifacts / f"{tag}.screen.txt").write_text("\n".join(read.rows()) + "\n", encoding="utf-8")
    return read


def cursor_items(timeout: float = 3.0) -> list[int]:
    """The listing items shown in reverse video, as indexes on the page: the cursor.
    An exact (DMA) read, which carries the reverse bit. The highlight follows the repaint,
    so this waits for one to appear."""
    deadline = time.monotonic() + timeout
    while True:
        rev = screen.read(exact=True).reverse().reshape(-1, SCREEN_COLUMNS)
        items = [r - FIRST_ITEM_ROW for r in range(FIRST_ITEM_ROW, FIRST_ITEM_ROW + ITEMS_PER_PAGE) if rev[r].any()]
        if items or time.monotonic() > deadline:
            return items
        time.sleep(0.1)


def goto(drive: str, *directories: str):
    """To a directory of ``drive``, each name matched as the card spells it, whatever its case."""
    listing = menu.select_source(drive)
    for name in directories:
        page, index = find_prefix(name)
        if page.names[index].lower() != name.lower():
            raise MenuError(f"{name!r} is not in {page.path}: {page.names}")
        listing = menu.open(page.names[index])
    return listing


def find_prefix(prefix: str):
    """The page listing an entry whose name starts with ``prefix`` and its index there."""
    listing = menu.read(exact=True)     # a video read can leave a cell unsure, and names are compared
    for _ in range(max(listing.page[1], 1)):
        for i, name in enumerate(listing.names):
            if name.lower().startswith(prefix.lower()):
                return listing, i
        if listing.page[1] <= 1:
            break
        following = listing.page[0] % listing.page[1] + 1
        menu.press(CRSR_RIGHT)
        listing = menu.wait(lambda l: l.page[0] == following, exact=True, what=f"page {following} of {listing.path}")
    raise MenuError(f"nothing starting with {prefix!r} in {listing.path}: {listing.names}")


def press_delete(tag: str) -> str:
    """INST/DEL on the cursor's item; the screen once the firmware has answered."""
    menu.press(DELETE)
    read = screen.wait(lambda s: PROMPT in s.text() or ANY_KEY in s.text(), exact=True,
                       what="the delete prompt or a refusal")
    return save(tag, read).text()


def delete_at_cursor(tag: str, shown: str) -> None:
    """INST/DEL, the prompt naming ``shown``, then y."""
    text = press_delete(f"{tag}-prompt")
    if PROMPT not in text:
        raise AssertionError(f"no y/n prompt:\n{text}")
    if "Delete File:" not in text or shown.lower() not in text.lower():
        raise AssertionError(f"the prompt does not name {shown}:\n{text}")
    menu.press(KEY_Y)


def names_on_drive(directory: str, drive: str = "sd") -> list[str]:
    return [e["name"] for e in link.list_directory(directory, drive)]


def remove_quietly(path: str, drive: str = "sd") -> None:
    try:
        link.delete_file(path, drive)
    except LinkError:
        pass


def back_to_menu() -> None:
    """Out of whatever a failed case left on the screen."""
    try:
        text = screen.read(exact=True).text()
        if PROMPT in text or ANY_KEY in text:
            menu.press(SPACE)               # not 'y': cancels the prompt, or answers "any key"
            time.sleep(1.0)
        menu.read()
    except Exception:
        board.ensure_menu()


def run(name: str, case) -> None:
    if SELECTED and name not in SELECTED:
        return
    try:
        cases[name] = case()
    except Exception as e:
        try:
            save(f"{name}-failure", screen.read(exact=True))
        except Exception:
            pass
        cases[name] = {"outcome": "error", "detail": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc().splitlines()[-6:]}
        back_to_menu()
    log(f"{name}: {cases[name]['outcome']} - {cases[name].get('detail', '')}")


# -- the cases ---------------------------------------------------------------

def only_file_subfolder() -> dict:
    try:
        stale = names_on_drive(DIR)            # a run that stopped early may have left files
    except LinkError:
        stale = []                             # the folder is not there yet; the post makes it
    for name in stale:
        remove_quietly(f"{DIR}/{name}")
    link.push_file(b"treadmill menu-delete test\n", f"{DIR}/{ONLY}")
    try:
        goto("sd", ROOT, "/" + FOLDER)
        listing, index = find_prefix(ONLY)
        if not (len(listing.names) == 2 and listing.names[0] == UP_DIR and same(listing.names[1], ONLY)):
            return {"outcome": "skip", "detail": f"{DIR} holds more than the test file: {listing.names}"}
        menu._reach(listing, index)
        delete_at_cursor("subfolder", ONLY)
        after = menu.wait(lambda l: not any(same(n, ONLY) for n in l.names), exact=True, what=f"{DIR} without {ONLY}")
        save("subfolder-after", screen.read(exact=True))
        cursor, left = cursor_items(), names_on_drive(DIR)
        good = after.names == [UP_DIR] and cursor == [0] and not any(same(n, ONLY) for n in left)
        return {"outcome": "pass" if good else "fail",
                "detail": f"listing {after.names}, cursor on item {cursor}, on the card {left}"}
    finally:
        remove_quietly(f"{DIR}/{ONLY}")


def usb_cases() -> None:
    """only_file_root and del_on_empty, on whatever the USB port has."""
    try:
        entries = link.list_directory("/", "usb")
        listed = f"{len(entries)} entries"
    except LinkError as e:
        entries, listed = None, f"no listing ({e})"
    if entries:
        skip = {"outcome": "skip", "detail": f"the USB drive has {len(entries)} entries; its root is not emptied for a test"}
        cases["only_file_root"], cases["del_on_empty"] = skip, dict(skip)
        return
    present = False
    try:
        link.post_file(b"treadmill menu-delete test\n", f"/{ONLY}", "usb")
        present = True
    except LinkError as e:
        cases["only_file_root"] = {"outcome": "skip", "detail": f"no writable USB drive ({listed}; post: {e})"}

    def only_file_root() -> dict:
        try:
            listing = menu.select_source("usb")
            listing, index = find_prefix(ONLY)
            if not (len(listing.names) == 1 and same(listing.names[0], ONLY)):
                return {"outcome": "skip", "detail": f"the USB root holds more than the test file: {listing.names}"}
            menu._reach(listing, index)
            delete_at_cursor("usbroot", ONLY)
            after = menu.wait(lambda l: not any(same(n, ONLY) for n in l.names), exact=True, what=f"the USB root without {ONLY}")
            save("usbroot-after", screen.read(exact=True))
            cursor, left = cursor_items(), names_on_drive("/", "usb")
            good = after.names == [EMPTY] and cursor == [0] and not left
            return {"outcome": "pass" if good else "fail",
                    "detail": f"listing {after.names}, cursor on item {cursor}, on the drive {left}"}
        finally:
            remove_quietly(f"/{ONLY}", "usb")

    def del_on_empty() -> dict:
        listing = menu.select_source("usb")
        if listing.names != [EMPTY]:
            return {"outcome": "skip", "detail": f"the USB source does not list {EMPTY}: {listing.names}"}
        text = press_delete("empty-refusal")
        refused = REFUSED_NOT_A_FILE in text and ANY_KEY in text and PROMPT not in text
        menu.press(SPACE)
        after = menu.wait(lambda l: l.names == [EMPTY], exact=True, what="the listing after the refusal")
        return {"outcome": "pass" if refused else "fail",
                "detail": ("refused with the reason and any-key" if refused else f"screen:\n{text}")
                          + f"; back at {after.names}; drive {'attached, empty' if present else 'absent'}"}

    if present:
        run("only_file_root", only_file_root)
    run("del_on_empty", del_on_empty)


def vms_claim() -> dict:
    try:
        folders = [e["name"].strip("/") for e in link.list_directory("/VMS") if e.get("type") != "file"]
    except LinkError as e:
        return {"outcome": "skip", "detail": f"no /VMS on the card ({e})"}
    claims: dict[str, list[str]] = {}          # extension -> the packages claiming it
    manifests: dict[str, tuple[str, bytes]] = {}   # package -> (the manifest's name on the card, its bytes)
    for folder in folders:
        try:
            stored = next(n for n in names_on_drive(f"/VMS/{folder}") if n.lower() == "manifest.vmi")
            data = link.get_file(f"/VMS/{folder}/{stored}")
        except (StopIteration, LinkError):
            continue
        lines = data.decode("latin-1").splitlines()
        if len(lines) < 3:
            continue
        manifests[folder] = (stored, data)
        for ext in (x.strip() for x in lines[2].split(",")):
            if ext.isalnum() and len(ext) <= 7:
                claims.setdefault(ext.lower(), []).append(folder)
    sole = sorted((ext, who[0]) for ext, who in claims.items() if len(who) == 1 and who[0] in manifests)
    if not sole:
        return {"outcome": "skip", "detail": f"no file type one package alone claims (packages: {sorted(manifests)})"}
    ext, package = sole[0]
    stored, manifest = manifests[package]
    path, probe = f"/VMS/{package}/{stored}", f"CLAIM.{ext}"
    (artifacts / f"{package}-{stored}").write_bytes(manifest)      # a copy off the card before it is touched
    link.push_file(b"treadmill menu-delete test\n", f"{DIR}/{probe}")

    def probe_mark() -> str:
        goto("sd", ROOT, "/" + FOLDER)
        listing, index = find_prefix("CLAIM")
        return listing.entries[index].mark

    try:
        before = probe_mark()
        if before.strip().lower() != ext[:3]:
            return {"outcome": "skip", "detail": f"{probe} lists as {before!r}, not claimed, before anything is deleted"}
        goto("sd", "/VMS", "/" + package)
        listing, index = find_prefix(stored)
        if listing.names[index].lower() != stored.lower():
            raise MenuError(f"{stored} is not listed in /VMS/{package}: {listing.names}")
        menu._reach(listing, index)
        delete_at_cursor("vms", stored)
        menu.wait(lambda l: not any(same(n, stored) for n in l.names), exact=True, what=f"/VMS/{package} without its manifest")
        gone = stored not in names_on_drive(f"/VMS/{package}")
        after = probe_mark()
        link.push_file(manifest, path)
        restored = probe_mark()
        good = gone and after == MARK_UNKNOWN and restored == before
        return {"outcome": "pass" if good else "fail",
                "detail": f"{probe} listed as {before!r}, then {after!r} with {package}'s manifest deleted "
                          f"(off the card: {gone}), then {restored!r} with it posted back"}
    finally:
        if stored not in names_on_drive(f"/VMS/{package}"):
            link.push_file(manifest, path)
        if link.get_file(path) != manifest:
            raise AssertionError(f"{path} on the card is not the manifest that was read; the copy is in the artifacts")
        remove_quietly(f"{DIR}/{probe}")


# -- run ---------------------------------------------------------------------

log(f"{station.id}: {board.banner.version} on a {board.banner.machine}")
if keys is None:
    result = {"outcome": "skip", "detail": "this station cannot press keys (a TR+ is needed)"}
else:
    run("only_file_subfolder", only_file_subfolder)
    if not SELECTED or SELECTED & {"only_file_root", "del_on_empty"}:
        usb_cases()
    run("vms_claim", vms_claim)
    back_to_menu()
    result = {"version": board.banner.version, "machine": board.banner.machine, "cases": cases,
              "passed": sorted(k for k, v in cases.items() if v["outcome"] == "pass"),
              "not_passed": {k: v["outcome"] for k, v in cases.items() if v["outcome"] != "pass"}}
