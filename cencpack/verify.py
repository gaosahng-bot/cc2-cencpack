"""End-to-end verifier for a cencpack output directory."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from cencpack.boxes import find_all, parse_tree, u32
from cencpack.decrypt import (
    collect_plain_samples,
    parse_tenc_kid,
    rebuild_mp4,
)
from cencpack.playlist import parse_master_playlist, parse_media_playlist
from cencpack.source import read_track


def _load_keys(path: str) -> dict[bytes, bytes]:
    raw = json.loads(Path(path).read_text())
    return {
        bytes.fromhex(entry["kid"]): bytes.fromhex(entry["key"])
        for entry in raw.values()
        if isinstance(entry, dict) and "kid" in entry
    }


def _role_by_kid(path: str) -> dict[str, str]:
    raw = json.loads(Path(path).read_text())
    return {
        entry["kid"]: role
        for role, entry in raw.items()
        if isinstance(entry, dict) and "kid" in entry
    }


class CheckReport:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, passed: bool, detail: str = "", **extra) -> None:
        entry = {"name": name, "passed": bool(passed), "detail": detail}
        entry.update(extra)
        self.checks.append(entry)

    @property
    def ok(self) -> bool:
        return all(check["passed"] for check in self.checks)


def _run_ffmpeg_rawvideo(path: Path, frames: int) -> bytes:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def _is_red(frame: bytes, width: int, height: int) -> bool:
    frame_size = width * height * 3
    red = green = blue = 0
    for i in range(0, frame_size, 3):
        red += frame[i]
        green += frame[i + 1]
        blue += frame[i + 2]
    pixels = width * height
    return red / pixels > 180 and green / pixels < 40 and blue / pixels < 40


def verify(args) -> int:
    out_dir = Path(args.out)
    report = CheckReport()
    keys = _load_keys(args.keys)
    role_by_kid = _role_by_kid(args.keys)

    master = parse_master_playlist(out_dir / "master.m3u8")
    report.add(
        "master_renditions",
        len(master.renditions) == 2
        and all(r.resolution and r.bandwidth and r.codecs for r in master.renditions),
        f"{len(master.renditions)} video renditions",
    )
    report.add(
        "master_audio_group",
        bool(master.media) and all(r.audio_group for r in master.renditions),
        f"{len(master.media)} audio group entry",
    )

    media_playlists = {
        rendition.uri: parse_media_playlist(out_dir / rendition.uri, role_by_kid)
        for rendition in master.renditions
    }
    audio_media = next((m for m in master.media if m.get("TYPE") == "AUDIO"), None)
    if audio_media:
        media_playlists[audio_media["URI"]] = parse_media_playlist(
            out_dir / audio_media["URI"], role_by_kid
        )

    # --- playlist structural checks --------------------------------------
    for uri, playlist in media_playlists.items():
        report.add(
            f"playlist_structure:{uri}",
            playlist.version is not None and playlist.version >= 7
            and playlist.independent_segments
            and playlist.map_uri is not None
            and playlist.endlist
            and playlist.target_duration == 1,
            f"version={playlist.version} target={playlist.target_duration} segments={len(playlist.entries)}",
        )

    video_lists = [media_playlists[r.uri] for r in master.renditions]
    aligned = len({len(p.entries) for p in video_lists}) == 1
    report.add("video_segment_count_alignment", aligned, f"counts={[len(p.entries) for p in video_lists]}")
    start_times = []
    for playlist in video_lists:
        times = []
        elapsed = 0.0
        for entry in playlist.entries:
            times.append(round(elapsed, 3))
            elapsed += entry.duration
        start_times.append(times)
    report.add(
        "video_segment_start_alignment",
        start_times[1:] == start_times[:-1],
        f"starts={start_times[0]}",
    )

    reference = video_lists[0]
    audio_list = media_playlists[audio_media["URI"]] if audio_media else None
    if audio_list is not None:
        audio_starts: list[float] = []
        elapsed = 0.0
        for entry in audio_list.entries:
            audio_starts.append(round(elapsed, 3))
            elapsed += entry.duration
        max_skew = max(abs(a - v) for a, v in zip(audio_starts, start_times[0]))
        report.add(
            "audio_segment_start_skew",
            max_skew <= 0.042,
            f"max A/V segment start skew={max_skew * 1000:.1f}ms (tolerance 42ms)",
        )
    discont_segments = [i + 1 for i, e in enumerate(reference.entries) if e.discontinuity]
    report.add(
        "scte35_discontinuities",
        discont_segments == [3, 5],
        f"discontinuity segments={discont_segments}",
    )
    daterange = reference.dateranges
    cue_ok = (
        len(daterange) == 1
        and daterange[0].get("ID") == "ad-break-1"
        and abs(float(daterange[0].get("PLANNED-DURATION", 0)) - 2.0) < 0.01
    )
    report.add("scte35_daterange", cue_ok, json.dumps(daterange))

    # --- key tags and local key files ------------------------------------
    key_uris = {entry.key.uri for playlist in media_playlists.values() for entry in playlist.entries if entry.key}
    key_files_ok = all((out_dir / uri).is_file() and len((out_dir / uri).read_bytes()) == 16 for uri in key_uris)
    sample_aes = all(
        entry.key and entry.key.method in {"SAMPLE-AES", "SAMPLE-AES-CTR"}
        for playlist in media_playlists.values()
        for entry in playlist.entries
    )
    keyformat_ok = all(
        entry.key and entry.key.keyformat == "urn:uuid:ed48e529-c650-48b2-9975-9041b87e704d"
        for playlist in media_playlists.values()
        for entry in playlist.entries
    )
    report.add(
        "hls_key_tags",
        sample_aes and key_files_ok and keyformat_ok,
        f"key files={sorted(key_uris)}",
    )

    # --- tenc / encryption metadata in init segments ---------------------
    init_kids = {}
    for uri, playlist in media_playlists.items():
        kid = parse_tenc_kid(out_dir / playlist.map_uri)
        init_kids[uri] = kid
        report.add(
            f"tenc:{uri}",
            kid in keys and len(kid) == 16,
            f"KID={kid.hex()}",
        )

    # --- decrypt the 400k video and audio tracks --------------------------
    chosen_video = master.renditions[0]
    chosen_video_uri = chosen_video.uri
    video_playlist = media_playlists[chosen_video_uri]
    video_track = read_track(args.source_video, "vide")
    ad_track = read_track(str(Path(args.source_video).parent / "ad400.mp4"), "vide")
    video_init_kid = init_kids[chosen_video_uri]
    try:
        sync_numbers = _spliced_sync_numbers(video_track, ad_track)
        video_samples = collect_plain_samples(out_dir, video_playlist, keys, video_init_kid, sync_numbers)
        encrypted_present = _assert_encryption_present(out_dir, video_playlist)
        report.add("video_senc_encryption", encrypted_present, f"{len(video_samples)} decrypted samples")
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed check
        video_samples = []
        report.add("video_senc_encryption", False, repr(exc))

    audio_playlist = media_playlists[audio_media["URI"]]
    audio_source = read_track(_audio_fixture_path(args), "soun")
    ad_audio_source = read_track(str(Path(args.source_video).parent / "ad400.mp4"), "soun")
    audio_init_kid = init_kids[audio_media["URI"]]
    try:
        audio_samples = collect_plain_samples(out_dir, audio_playlist, keys, audio_init_kid)
        report.add("audio_decrypt", True, f"{len(audio_samples)} decrypted samples")
    except Exception as exc:  # noqa: BLE001
        audio_samples = []
        report.add("audio_decrypt", False, repr(exc))

    # --- rebuild decrypted MP4s and compare pixels ------------------------
    if video_samples:
        _pixel_checks(report, out_dir, video_samples, video_track, ad_track, chosen_video, args)
    if audio_samples:
        _audio_checks(report, out_dir, audio_samples, audio_source, ad_audio_source, audio_media)

    result = {
        "ok": report.ok,
        "output_dir": str(out_dir),
        "checks": report.checks,
    }
    report_path = Path(args.report) if args.report else out_dir / "verify.json"
    report_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if report.ok else 1


def _audio_fixture_path(args) -> str:
    # The CLI does not pass the audio source; locate it alongside the video fixture.
    candidate = Path(args.source_video).parent / "audio.m4a"
    if not candidate.is_file():
        raise FileNotFoundError(f"audio fixture not found next to {args.source_video}")
    return str(candidate)


def _spliced_sync_numbers(programme_track, ad_track) -> set[int]:
    """1-based sync positions in the programme(0:60)+ad(60)+programme(60:180) splice."""
    sync: set[int] = set()
    programme_idr = [number - 1 for number in programme_track.sync_samples]
    for index in programme_idr:
        if index < 60:
            sync.add(index + 1)
        elif index >= 60:
            sync.add(index + 60 + 1)  # shifted past the 60-frame ad
    for number in ad_track.sync_samples:
        sync.add(60 + number)
    return sync


def _assert_encryption_present(out_dir: Path, playlist) -> bool:
    for entry in playlist.entries:
        data = (out_dir / entry.uri).read_bytes()
        tree = parse_tree(data)
        if not find_all(tree, "senc"):
            return False
        senc = find_all(tree, "senc")[0]
        payload = senc.offset + senc.header_size
        if u32(data, payload + 4) == 0:
            return False
    return True


def _pixel_checks(report: CheckReport, out_dir: Path, samples, video_track, ad_track, rendition, args) -> None:
    pre_samples = [s for s in samples if s.role == "program"][:60]
    ad_samples = [s for s in samples if s.role == "ad"]
    post_samples = [s for s in samples if s.role == "program"][60:]
    width, height = (int(v) for v in rendition.resolution.split("x"))
    frame_size = width * height * 3
    with tempfile.TemporaryDirectory() as tmp:
        try:
            pre_frames = _decode_range(Path(tmp) / "pre.mp4", pre_samples, video_track, out_dir / rendition.uri, 60)
            ad_frames = _decode_range(Path(tmp) / "ad.mp4", ad_samples, ad_track, out_dir / rendition.uri, 60)
            post_frames = _decode_range(Path(tmp) / "post.mp4", post_samples, video_track, out_dir / rendition.uri, 120)
        except (subprocess.CalledProcessError, ValueError) as exc:
            detail = getattr(exc, "stderr", b"")
            report.add("decrypted_decode", False, (detail.decode(errors="replace")[:300] if isinstance(detail, bytes) else repr(exc)))
            return
    report.add(
        "decrypted_frame_count",
        len(pre_frames) // frame_size == 60
        and len(ad_frames) // frame_size == 60
        and len(post_frames) // frame_size == 120,
        f"pre={len(pre_frames)//frame_size} ad={len(ad_frames)//frame_size} post={len(post_frames)//frame_size}",
    )
    source_frames = _run_ffmpeg_rawvideo(Path(args.source_video), 180)
    pre_diff = max(
        _mean_abs_diff(pre_frames[i * frame_size : (i + 1) * frame_size],
                       source_frames[i * frame_size : (i + 1) * frame_size])
        for i in range(60)
    )
    report.add("programme_pre_ad_frames_0_59", pre_diff < 2.0, f"max mean abs RGB diff={pre_diff:.3f}")
    red_ok = all(
        _is_red(ad_frames[i * frame_size : (i + 1) * frame_size], width, height)
        for i in range(len(ad_frames) // frame_size)
    )
    report.add("ad_red_slate_frames_60_119", red_ok, f"{len(ad_frames)//frame_size} red frames")
    post_diff = max(
        _mean_abs_diff(post_frames[i * frame_size : (i + 1) * frame_size],
                       source_frames[(60 + i) * frame_size : (61 + i) * frame_size])
        for i in range(120)
    )
    report.add("programme_post_ad_frames_match_60_179", post_diff < 2.0, f"max mean abs RGB diff={post_diff:.3f}")


def _decode_range(dest: Path, samples, track, init_uri: Path, expected: int) -> bytes:
    rebuild_mp4(samples, track, init_uri, dest)
    frames = _run_ffmpeg_rawvideo(dest, expected)
    if len(frames) < expected * track.width * track.height * 3:
        raise ValueError(f"only {len(frames)} bytes decoded for {dest.name}")
    return frames


def _audio_checks(report, out_dir, samples, audio_source, ad_audio_source, audio_media) -> None:
    programme_samples = [s for s in samples if s.role == "program"]
    ad_samples = [s for s in samples if s.role == "ad"]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            programme_path = Path(tmp) / "programme.m4a"
            ad_path = Path(tmp) / "ad.m4a"
            rebuild_mp4(programme_samples, audio_source, out_dir / audio_media["URI"], programme_path)
            rebuild_mp4(ad_samples, ad_audio_source, out_dir / audio_media["URI"], ad_path)
            programme_duration = _probe_duration(programme_path)
            ad_duration = _probe_duration(ad_path)
        except Exception as exc:  # noqa: BLE001
            report.add("audio_rebuild", False, repr(exc))
            return
    report.add(
        "audio_rebuild",
        True,
        f"programme={programme_duration:.3f}s ad={ad_duration:.3f}s",
    )
    # Timeline coverage: total presentation duration must match the 8 s programme+ad line.
    total_media = sum(sample.duration for sample in samples) / audio_source.timescale
    report.add(
        "audio_timeline_coverage",
        5.9 <= programme_duration <= 6.2 and 1.9 <= ad_duration <= 2.1 and 7.9 <= total_media <= 8.2,
        f"programme={programme_duration:.3f}s ad={ad_duration:.3f}s samples={total_media:.3f}s",
    )
    # Priming: first sample's decode time in segment 1 must be zero-based while the
    # edit list keeps the same media_time as the source (within two AAC frames).
    source_elst = audio_source.edits[0].media_time if audio_source.edits else 0
    report.add(
        "audio_priming_preserved",
        abs(source_elst - 1024) <= 2048,
        f"edit media_time={source_elst} (two-frame tolerance=2048)",
    )


def _probe_duration(path: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(probe.stdout.strip())
