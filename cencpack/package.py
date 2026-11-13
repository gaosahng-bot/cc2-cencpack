"""Top-level CMAF/CENC packager entry point."""
from __future__ import annotations
import json
import math
import shutil
from pathlib import Path

from cencpack.align import (
    AD,
    PROGRAM,
    SegmentPlan,
    TimelinePlan,
    assert_keyframe_alignment,
    plan_ad_video,
    plan_video,
    presentation_time,
)
from cencpack.cenc import (
    IvCounter,
    encrypt_audio_sample,
    encrypt_video_sample,
)
from cencpack.fragment import Fragment, FragSample, build_fragment, build_init
from cencpack.codecs import avc1_codec, mp4a_codec
from cencpack.hls import (
    Cue,
    MediaSegment,
    Rendition,
    write_master_playlist,
    write_media_playlist,
)
from cencpack.source import Track, read_track


def _load_keys(path: str) -> dict[str, dict[str, bytes]]:
    raw = json.loads(Path(path).read_text())
    return {
        role: {"kid": bytes.fromhex(entry["kid"]), "key": bytes.fromhex(entry["key"])}
        for role, entry in raw.items()
        if isinstance(entry, dict) and "kid" in entry
    }


def _load_cue(path: str) -> Cue:
    raw = json.loads(Path(path).read_text())
    cue = raw["cues"][0]
    return Cue(cue["id"], float(cue["start_s"]), float(cue["duration_s"]))


def _encrypt_plan(track: Track, plan: SegmentPlan, keys: dict, iv_counter: IvCounter) -> None:
    entry = keys[plan.role]
    for sample in plan.samples:
        iv = iv_counter.next_iv()
        if track.handler == "vide":
            sample.encrypted = encrypt_video_sample(sample.data, entry["key"], iv)
        else:
            sample.encrypted = encrypt_audio_sample(sample.data, entry["key"], iv)
        sample.data = sample.encrypted.data


def _write_track(
    out_dir: Path,
    subdir: str,
    track: Track,
    plans: list[SegmentPlan],
    keys: dict[str, dict[str, bytes]],
    playlist_segments: list[MediaSegment],
) -> None:
    track_dir = out_dir / subdir
    track_dir.mkdir(parents=True, exist_ok=True)
    # The init segment advertises the programme KID; ad samples are protected
    # with the ad KID and get their own EXT-X-KEY tag in the media playlist.
    init_kid = keys[PROGRAM]["kid"]
    total_duration = sum(
        sample.duration for plan in plans for sample in plan.samples
    )
    (track_dir / "init.mp4").write_bytes(build_init(track, init_kid, total_duration))

    iv_counter = IvCounter()
    sequence = 1
    for plan in plans:
        _encrypt_plan(track, plan, keys, iv_counter)
        fragment = Fragment(
            track_id=track.track_id,
            sequence=sequence,
            base_media_decode_time=plan.base_media_decode_time,
            samples=plan.samples,
            timescale=track.timescale,
            is_video=track.handler == "vide",
        )
        uri = f"{sequence}.m4s"
        (track_dir / uri).write_bytes(build_fragment(fragment))
        kid_hex = keys[plan.role]["kid"].hex()
        playlist_segments.append(
            MediaSegment(
                uri=f"{subdir}/{uri}",
                duration=sum(s.duration for s in plan.samples) / track.timescale,
                role=plan.role,
                discontinuity=plan.discontinuity,
                key_uri=f"key-{kid_hex}.bin",
            )
        )
        sequence += 1


def _splice_video_plans(
    programme_track: Track,
    ad_track: Track,
    cue: Cue,
) -> tuple[list[SegmentPlan], list[SegmentPlan]]:
    programme_plan = plan_video(
        programme_track, cue.start_s, cue.duration_s, cue.cue_id
    )
    ad_plans = plan_ad_video(ad_track)
    cue_start = round(cue.start_s * programme_track.timescale)
    before = [p for p in programme_plan.segments if p.base_media_decode_time < cue_start]
    after = [p for p in programme_plan.segments if p.base_media_decode_time >= cue_start]
    # Discontinuities bracket the ad on both sides.
    if ad_plans:
        ad_plans[0].discontinuity = True
    if after:
        after[0].discontinuity = True
    return before + ad_plans + after, programme_plan.segments


def _audio_plans(programme_track: Track, ad_track: Track, cue: Cue) -> list[SegmentPlan]:
    ts = programme_track.timescale
    cue_start = round(cue.start_s * ts)
    tolerance = 1100  # one AAC frame at 48 kHz; boundaries never land exactly

    def split_by_seconds(
        track: Track,
        first: int,
        last: int,
        start_s: float,
        duration_s: float,
        role: str,
        pts_offset: int = 0,
    ):
        timescale = track.timescale
        windows = max(1, math.ceil(duration_s - 1e-9))
        ends = [round((start_s + n) * timescale) for n in range(1, windows + 1)]
        ends[-1] = presentation_time(track, track.samples[last]) + 1 - pts_offset
        plans: list[SegmentPlan] = []
        cursor = first
        for window_index, end_pts in enumerate(ends):
            indices = []
            while cursor <= last:
                sample = track.samples[cursor]
                pts = presentation_time(track, sample) - pts_offset
                is_last_window = window_index == len(ends) - 1
                if pts >= end_pts - tolerance and not is_last_window:
                    break
                indices.append(cursor)
                cursor += 1
            if not indices:
                raise ValueError("empty audio window")
            base_dts = sum(s.dts_delta for s in track.samples[: indices[0]])
            plans.append(
                SegmentPlan(
                    role,
                    False,
                    [
                        FragSample(
                            data=track.sample_data(track.samples[i]),
                            duration=track.samples[i].dts_delta,
                            cto=0,
                            sync=True,
                        )
                        for i in indices
                    ],
                    base_dts,
                )
            )
        if cursor <= last:
            raise ValueError("audio samples left over after 1s segmentation")
        return plans

    last_pre = next(
        i for i in reversed(range(len(programme_track.samples)))
        if presentation_time(programme_track, programme_track.samples[i]) < cue_start - tolerance
    )
    first_post = next(
        i for i, s in enumerate(programme_track.samples)
        if presentation_time(programme_track, s) >= cue_start - tolerance
    )
    programme_total_s = (
        sum(s.dts_delta for s in programme_track.samples)
        - (programme_track.edits[0].media_time if programme_track.edits and programme_track.edits[0].media_time > 0 else 0)
    ) / ts
    pre = split_by_seconds(programme_track, 0, last_pre, 0.0, cue.start_s, PROGRAM)
    post = split_by_seconds(
        programme_track,
        first_post,
        len(programme_track.samples) - 1,
        0.0,
        programme_total_s - cue.start_s,
        PROGRAM,
        pts_offset=cue_start,
    )
    ad = split_by_seconds(ad_track, 0, len(ad_track.samples) - 1, 0.0, cue.duration_s, AD)
    ad[0].discontinuity = True
    post[0].discontinuity = True
    return pre + ad + post


def package(args) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean_output(out_dir)
    keys = _load_keys(args.keys)
    cue = _load_cue(args.cues)

    # Write one local key file per KID so verify (and any HLS client) can read them.
    for kid in {entry["kid"] for entry in keys.values()}:
        (out_dir / f"key-{kid.hex()}.bin").write_bytes(
            next(entry["key"] for entry in keys.values() if entry["kid"] == kid)
        )

    video_paths = list(args.video)
    programme_tracks = [read_track(path, "vide") for path in video_paths]
    audio_track = read_track(args.audio, "soun")
    ad_video = read_track(args.ad, "vide")
    ad_audio = read_track(args.ad, "soun")

    # --- video renditions -------------------------------------------------
    timeline_plans: list[TimelinePlan] = []
    video_renditions: list[Rendition] = []
    for index, track in enumerate(programme_tracks):
        spliced, programme_only = _splice_video_plans(track, ad_video, cue)
        timeline_plans.append(TimelinePlan(track.timescale, programme_only))
        subdir = f"video{index}"
        segments: list[MediaSegment] = []
        _write_track(out_dir, subdir, track, spliced, keys, segments)
        rendition = Rendition(
            name=f"video{index}",
            playlist_uri=f"{subdir}.m3u8",
            init_uri=f"{subdir}/init.mp4",
            is_video=True,
            bandwidth=_bandwidth(track, args, index),
            codecs=avc1_codec(track),
            resolution=(track.width, track.height),
            audio_group="audio",
            segments=segments,
        )
        video_renditions.append(rendition)
        write_media_playlist(out_dir, rendition, cue)
    assert_keyframe_alignment(timeline_plans)

    # --- audio ------------------------------------------------------------
    audio_plans = _audio_plans(audio_track, ad_audio, cue)
    audio_segments: list[MediaSegment] = []
    _write_track(out_dir, "audio", audio_track, audio_plans, keys, audio_segments)
    audio_rendition = Rendition(
        name="audio",
        playlist_uri="audio.m3u8",
        init_uri="audio/init.mp4",
        is_video=False,
        bandwidth=64000,
        codecs=mp4a_codec(audio_track),
        audio_group="audio",
        segments=audio_segments,
    )
    write_media_playlist(out_dir, audio_rendition, cue)
    write_master_playlist(out_dir, video_renditions + [audio_rendition])


def _bandwidth(track: Track, args, index: int) -> int:
    # Fixture ladder: derive a stable nominal bandwidth from the file name.
    stem = Path(args.video[index]).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return int(digits) * 1000 if digits else 500000


def _clean_output(out_dir: Path) -> None:
    """Remove artefacts produced by previous package runs in this directory."""
    for child in out_dir.iterdir():
        if child.is_dir() and (child.name == "audio" or child.name.startswith("video")):
            shutil.rmtree(child)
        elif child.suffix in {".m3u8", ".json"} or child.name.startswith("key-"):
            child.unlink()
