"""Minimal HLS playlist reader used by the verifier."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class KeyTag:
    method: str
    uri: str
    keyformat: str | None = None


@dataclass
class PlaylistEntry:
    uri: str
    duration: float
    discontinuity: bool = False
    key: KeyTag | None = None
    role: str = "program"


@dataclass
class MediaPlaylist:
    path: Path
    version: int | None = None
    target_duration: int | None = None
    independent_segments: bool = False
    map_uri: str | None = None
    endlist: bool = False
    entries: list[PlaylistEntry] = field(default_factory=list)
    dateranges: list[dict] = field(default_factory=list)


def _parse_attrs(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    token = ""
    in_quote = False
    pairs: list[str] = []
    for char in text:
        if char == '"':
            in_quote = not in_quote
            token += char
        elif char == "," and not in_quote:
            pairs.append(token)
            token = ""
        else:
            token += char
    pairs.append(token)
    for pair in pairs:
        if "=" in pair:
            name, value = pair.split("=", 1)
            attrs[name.strip()] = value.strip().strip('"')
    return attrs


def parse_media_playlist(path: str | Path, role_by_kid: dict[str, str] | None = None) -> MediaPlaylist:
    role_by_kid = role_by_kid or {}
    path = Path(path)
    playlist = MediaPlaylist(path=path)
    current_key: KeyTag | None = None
    pending_discontinuity = False
    pending_inf: float | None = None
    pending_role = "program"
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "#EXTM3U":
            continue
        if line.startswith("#EXT-X-VERSION:"):
            playlist.version = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            playlist.target_duration = int(line.split(":", 1)[1])
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            playlist.independent_segments = True
        elif line == "#EXT-X-ENDLIST":
            playlist.endlist = True
        elif line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            current_key = KeyTag(attrs["METHOD"], attrs["URI"], attrs.get("KEYFORMAT"))
            kid = Path(attrs["URI"]).name.removeprefix("key-").removesuffix(".bin")
            pending_role = role_by_kid.get(kid, "program")
        elif line.startswith("#EXT-X-MAP:"):
            playlist.map_uri = _parse_attrs(line.split(":", 1)[1])["URI"]
        elif line.startswith("#EXT-X-DATERANGE:"):
            playlist.dateranges.append(_parse_attrs(line.split(":", 1)[1]))
        elif line.startswith("#EXTINF:"):
            pending_inf = float(line.split(":", 1)[1].rstrip(","))
        elif not line.startswith("#"):
            if pending_inf is None:
                raise ValueError(f"segment {line} without EXTINF")
            playlist.entries.append(
                PlaylistEntry(
                    uri=line,
                    duration=pending_inf,
                    discontinuity=pending_discontinuity,
                    key=current_key,
                    role=pending_role,
                )
            )
            pending_inf = None
            pending_discontinuity = False
    return playlist


@dataclass
class MasterRendition:
    bandwidth: int
    codecs: str
    resolution: str | None
    audio_group: str | None
    uri: str


@dataclass
class MasterPlaylist:
    media: list[dict] = field(default_factory=list)
    renditions: list[MasterRendition] = field(default_factory=list)


def parse_master_playlist(path: str | Path) -> MasterPlaylist:
    master = MasterPlaylist()
    pending: dict | None = None
    for raw_line in Path(path).read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            master.media.append(_parse_attrs(line.split(":", 1)[1]))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = _parse_attrs(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending is not None:
            master.renditions.append(
                MasterRendition(
                    bandwidth=int(pending["BANDWIDTH"]),
                    codecs=pending["CODECS"],
                    resolution=pending.get("RESOLUTION"),
                    audio_group=pending.get("AUDIO"),
                    uri=line,
                )
            )
            pending = None
    return master
