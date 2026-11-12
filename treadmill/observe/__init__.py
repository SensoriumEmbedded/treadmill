"""Seeing the machine: frames in, screen text and liveness out.

``capture`` gets frames; ``calibrate`` finds the text area in them;
``normalize`` turns it into 16x16 cells; ``glyphs`` says which screen code
each cell shows; ``channels`` is the one read (video or DMA) and the policy
between them; ``liveness`` says whether the machine is alive; ``golden``
compares screens with text goldens; ``render`` makes frames from screen
codes for the self-tests and for refining a calibration. ``sound`` measures
recordings of the C64's audio: tones, and a tune against a reference.
"""

from treadmill.observe.calibrate import (  # noqa: F401
    LAYOUT_BASIC, LAYOUT_MENU, Calibration, CalibrationError, calibration_path, find_text_area, refine, track,
)
from treadmill.observe.capture import (  # noqa: F401
    CaptureError, CaptureTimeout, CvCapture, Frame, FrameSource, ReplaySource, SyntheticSource,
    open_capture, save_strip,
)
from treadmill.observe.channels import (  # noqa: F401
    SOURCE_DMA, SOURCE_VIDEO, ScreenChannel, ScreenError, ScreenRead, ScreenTimeout, XCheck, from_dump,
    read_video, xcheck,
)
from treadmill.observe.glyphs import (  # noqa: F401
    Font, GlyphBank, GlyphError, bootstrap_bank, harvest, load_chargen, match, synthetic_font,
)
from treadmill.observe.golden import Golden, Mismatch, compare, diff_text, save_golden  # noqa: F401
from treadmill.observe.liveness import Liveness, Verdict, classify  # noqa: F401
from treadmill.observe.sound import (  # noqa: F401
    Comparison, Recording, SoundError, Tone, compare_audio, level, load_wav, measure_tones, pitch_ratio,
    resample, save_wav,
)
