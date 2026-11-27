"""
The face motion for Lumi's web Stage (Phase 3): the same motion engine that drives
VTube Studio, but with Live2D parameter names and no VTS connection.

VtubeStudio.compute_frame is the one place that decides what the face does (sway,
gaze, blinks, breath, emotion, lip-sync, yawns). StageMotion borrows those methods
and their state, so the Stage moves exactly like the VTS avatar does, while
VtubeStudio's constructor (which opens a connection and marks VTS as skipped) never
runs.

    StageMotion(signals).frame()           {Live2D param id: value}, for this instant
    StageMotion(signals).touch(zone, action) -> avatar_touch.Reaction | None
        the reaction's emotion is applied to the motion engine right away.

Costs nothing while nothing is subscribed: socketIO only asks for frames while a
Stage is open.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from modules import avatar_touch
from modules.vtubeStudio import VtubeStudio

# Live2D (Cubism) parameter ids for the slots compute_frame fills. The Ariu model uses these.
CUBISM_HEAD = {"x": "ParamAngleX", "y": "ParamAngleY", "z": "ParamAngleZ",
               "el": "ParamEyeLOpen", "er": "ParamEyeROpen"}
CUBISM_EXTRA = {"bx": "ParamBodyAngleX", "by": "ParamBodyAngleY", "bz": "ParamBodyAngleZ",
                "ex": "ParamEyeBallX", "ey": "ParamEyeBallY",
                "bly": "ParamBrowLY", "bry": "ParamBrowRY", "blf": "ParamBrowLForm", "brf": "ParamBrowRForm"}
CUBISM_MOUTH = "ParamMouthOpenY"
CUBISM_SMILE = "ParamMouthForm"
CUBISM_BREATH = "ParamBreath"


class StageMotion:
    # Borrowed from VtubeStudio: the frame is computed by the same code the VTS avatar uses.
    _init_motion = VtubeStudio._init_motion
    compute_frame = VtubeStudio.compute_frame
    _compute_brows = VtubeStudio._compute_brows
    _compute_arms = VtubeStudio._compute_arms
    _gaze_mode = VtubeStudio._gaze_mode

    def __init__(self, signals):
        self.signals = signals
        self._agent_snapshot: Dict = {}
        self._init_motion()
        self._head_names = dict(CUBISM_HEAD)
        self._extra_names = dict(CUBISM_EXTRA)
        self._mouth_name = CUBISM_MOUTH
        self._smile_name = CUBISM_SMILE
        self._breath_name = CUBISM_BREATH
        self._gate = avatar_touch.TouchGate()

    def frame(self, now: Optional[float] = None) -> Dict[str, float]:
        now = time.time() if now is None else now
        speaking = bool(getattr(self.signals, "AI_speaking", False))
        return self.compute_frame(now, speaking)

    def touch(self, zone: str, action: str, now: Optional[float] = None):
        now = time.time() if now is None else now
        reaction = avatar_touch.on_avatar_interact(zone, action, now, gate=self._gate)
        if reaction is not None and reaction.emotion:
            self._emotion.trigger(reaction.emotion, VtubeStudio.emotion_channels(reaction.emotion), now)
        return reaction
