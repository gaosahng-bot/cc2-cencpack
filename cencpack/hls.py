"""HLS master and media playlist generation (CMAF, SAMPLE-AES-CTR)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MediaSegment:
    uri: str
    duration: float
    role: str  # "program" / "ad"
    discontinuity: bool
    key_uri: str


@dataclass
class Rendition:
    name: str
    playlist_uri: str
    init_uri: str
    is_video: bool
    bandwidth: int
    codecs: str
    resolution: tuple[int, int] | None = None
    audio_group: str | None = None
    language: str = "und"
    channels: str = "2"
    segments: list[MediaSegment] = field(default_factory=list)


@dataclass
class Cue:
    cue_id: str
    start_s: float
    duration_s: float


def _key_tag(key_uri: str) -> str:
    return (
        '#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,'
        f'URI="{key_uri}",KEYFORMAT="urn:uuid:ed48e529-c650-48b2-9975-9041b87e704d",'
        'KEYFORMATVERSIONS="1"'
    )


def write_media_playlist(out_dir: str | Path, rendition: Rendition, cue: Cue | None) -> str:
    max_duration = max((s.duration for s in rendition.segments), default=1.0)
    # CMAF AAC segments run a frame (~21 ms) over the nominal 1.0 s boundary.
    target_duration = max(1, math.ceil(max_duration - 0.05))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:1",
        f'#EXT-X-MAP:URI="{rendition.init_uri}"',
    ]
    # One EXT-X-KEY per distinct key URI, re-emitted on key changes.
    previous_key: str | None = None
    daterange_emitted = False
    elapsed = 0.0
    for segment in rendition.segments:
        if segment.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if segment.key_uri != previous_key:
            lines.append(_key_tag(segment.key_uri))
            previous_key = segment.key_uri
        if cue is not None and not daterange_emitted and segment.role == "ad":
            start_date = (
                datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=elapsed)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            lines.append(
                f'#EXT-X-DATERANGE:ID="{cue.cue_id}",CLASS="com.apple.hls.interstitial",'
                f'START-DATE="{start_date}",PLANNED-DURATION={cue.duration_s:.3f},'
                'SCTE35-CMD=0x00000000,SCTE35-IN=0x00000000'
            )
            daterange_emitted = True
        lines.append(f"#EXTINF:{segment.duration:.3f},")
        lines.append(segment.uri)
        elapsed += segment.duration
    lines.append("#EXT-X-ENDLIST")
    path = Path(out_dir) / rendition.playlist_uri
    path.write_text("\n".join(lines) + "\n")
    if cue is not None and not daterange_emitted:
        raise ValueError("cue never appeared on the timeline")
    return str(path)


def write_master_playlist(out_dir: str | Path, renditions: list[Rendition]) -> str:
    videos = [r for r in renditions if r.is_video]
    audios = [r for r in renditions if not r.is_video]
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    groups = {r.audio_group for r in videos if r.audio_group}
    for group in sorted(groups):
        audio = next(r for r in audios if r.audio_group == group)
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{group}",NAME="{audio.name}",'
            f'DEFAULT=YES,AUTOSELECT=YES,LANGUAGE="{audio.language}",'
            f'CHANNELS="{audio.channels}",URI="{audio.playlist_uri}"'
        )
    for video in videos:
        attrs = [
            f"BANDWIDTH={video.bandwidth}",
            f"CODECS=\"{video.codecs}\"",
            f"RESOLUTION={video.resolution[0]}x{video.resolution[1]}",
        ]
        if video.audio_group:
            attrs.append(f'AUDIO="{video.audio_group}"')
        lines.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
        lines.append(video.playlist_uri)
    path = Path(out_dir) / "master.m3u8"
    path.write_text("\n".join(lines) + "\n")
    return str(path)
