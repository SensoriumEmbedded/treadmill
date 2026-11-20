"""The probes a sweep takes its samples with, one module each.

A probe's ``sample(label, setting)`` returns ``(failures, samples, note)``
or a ``Sample`` at the knob's current value; ``setting`` lets a probe that
reads its counters by DMA step back to the defaults for the read when the
knob under test times DMA (``with setting.at_defaults(): ...``). A probe
with ``companion = True`` runs beside the others instead: ``begin(label)``
before their samples, ``end()`` after, as the screen-noise probe does.
"""

from treadmill.probes.idread import IdReadProbe
from treadmill.probes.menu import MenuProbe

__all__ = ["IdReadProbe", "MenuProbe"]
