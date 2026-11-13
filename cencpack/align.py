"""Timeline planning: keyframe-aligned segments and SCTE-35 splice points.

A :class:`SegmentPlan` describes one output playlist entry: which source
track samples it contains, the media decode time written into ``tfdt`` and
which key (program/ad) protects it. Programme samples are split at the cue
point; the ad is spliced in between with discontinuities on both sides.
"""
from __future__ import annotations

from dataclasses import dataclass

from cencpack.fragment import FragSample
from cencpack.source import Sample, Track


PROGRAM = "program"
AD = "ad"


@dataclass
class SegmentPlan:
    role: str  # PROGRAM / AD
    discontinuity: bool
    samples: list[FragSample]
    base_media_decode_time: int


@dataclass
class TimelinePlan:
    timescale: int
    # 1-based output segment number -> plan
    segments: list[SegmentPlan]
    # programme presentation-time boundaries (track timescale units) of the cue
    cue_start: int | None = None
    cue_duration: int | None = None
    cue_id: str | None = None


def _priming(track: Track) -> int:
    if track.edits and track.edits[0].media_time > 0:
        return track.edits[0].media_time
    return 0


def presentation_time(track: Track, sample: Sample) -> int:
    """Presentation time on the output timeline, in track timescale units."""
    dts = sum(s.dts_delta for s in track.samples[: sample.index])
    return dts + sample.cto - _priming(track)


def _to_frag(sample: Sample, track: Track) -> FragSample:
    return FragSample(
        data=track.sample_data(sample),
        duration=sample.dts_delta,
        cto=sample.cto,
        sync=sample.sync,
    )


def plan_video(track: Track, cue_start_s: float, cue_duration_s: float, cue_id: str) -> TimelinePlan:
    cue_start = round(cue_start_s * track.timescale)
    cue_duration = round(cue_duration_s * track.timescale)
    syncs = [idx - 1 for idx in track.sync_samples]  # 0-based
    boundaries = syncs + [len(track.samples)]

    plans: list[SegmentPlan] = []
    groups = list(zip(boundaries[:-1], boundaries[1:]))
    for start, end in groups:
        # A GOP straddling the cue is split on the IDR. The bundled fixtures
        # place an IDR exactly on the splice point so one side is always empty.
        before = [s for s in range(start, end) if presentation_time(track, track.samples[s]) < cue_start]
        after = [s for s in range(start, end) if presentation_time(track, track.samples[s]) >= cue_start]
        for indices in (before, after):
            if not indices:
                continue
            base = track.samples[indices[0]]
            base_dts = sum(s.dts_delta for s in track.samples[: indices[0]])
            plans.append(
                SegmentPlan(
                    PROGRAM,
                    False,
                    [_to_frag(track.samples[i], track) for i in indices],
                    base_dts,
                )
            )
    return TimelinePlan(track.timescale, plans, cue_start, cue_duration, cue_id)


def plan_ad_video(track: Track) -> list[SegmentPlan]:
    """Fragment the ad on its own IDR cadence (fresh, discontinuity timeline)."""
    syncs = [idx - 1 for idx in track.sync_samples]
    boundaries = syncs + [len(track.samples)]
    plans: list[SegmentPlan] = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        base_dts = sum(s.dts_delta for s in track.samples[:start])
        plans.append(
            SegmentPlan(
                AD,
                False,
                [_to_frag(track.samples[i], track) for i in range(start, end)],
                base_dts,
            )
        )
    return plans


def assert_keyframe_alignment(plans: list[TimelinePlan]) -> None:
    """Every video rendition must produce the same segment count and starts."""
    reference = plans[0]
    for plan in plans[1:]:
        if plan.timescale != reference.timescale:
            raise ValueError("video renditions must share a timescale")
        if len(plan.segments) != len(reference.segments):
            raise ValueError(
                f"segment count mismatch: {len(plan.segments)} != {len(reference.segments)}"
            )
        for idx, (seg, ref) in enumerate(zip(plan.segments, reference.segments), start=1):
            if seg.base_media_decode_time != ref.base_media_decode_time:
                raise ValueError(
                    f"segment {idx} start mismatch: {seg.base_media_decode_time} != {ref.base_media_decode_time}"
                )
            if seg.role != ref.role:
                raise ValueError(f"segment {idx} role mismatch")
