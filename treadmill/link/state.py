"""Where the board is, from what it answers.

There is no state query in the firmware yet, so the state is derived: the
firmware check tells main from minimal from silent, the banner's ``Boot:``
line tells booting from booted, and a ping that answers ``Busy!`` means a
launched program's handler has replaced the menu's.
"""

from __future__ import annotations

from dataclasses import dataclass

from treadmill.link.trlink import Link, NoReply, Refused

SILENT = "silent"            # nothing answered: an extension image, or no board
MINIMAL = "minimal"          # MinimalBoot: reset, launch, version, firmware check only
MENU_BOOTING = "menu_booting"
MENU_READY = "menu_ready"
RUNNING = "running"          # a launched program; the handler's name when known


@dataclass(frozen=True)
class State:
    kind: str
    handler: str | None = None
    image: str | None = None

    @property
    def at_menu(self) -> bool:
        return self.kind == MENU_READY


def derive_state(link: Link, last_handler: str | None = None) -> State:
    image = link.fw_check()
    if image is None:
        return State(SILENT)
    if image == "minimal":
        return State(MINIMAL, image=image)
    banner = link.banner()
    if banner.boot == "in progress":
        return State(MENU_BOOTING, image=image)
    try:
        link.ping()
    except Refused as e:
        if "Busy" in e.text:
            return State(RUNNING, handler=last_handler, image=image)
        raise
    except NoReply:
        return State(SILENT, image=image)
    return State(MENU_READY, image=image)
