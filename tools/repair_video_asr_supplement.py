#!/usr/bin/env python3
"""Repair a missing video ASR source without rewriting the original event.

The command downloads the requested video, runs only the audio transcription
track, archives a new supplement event, and links it to the original event with
an ``updates`` edge. Temporary media is removed only after both writes verify.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bilibot.app.config_loader import ConfigLoader, is_sensitive_placeholder
from bilibot.bilibili_api import BilibiliAPI
from bilibot.llm.manager import LLMManager
from bilibot.memory_brain import MemoryBrainService
from bilibot.memory_brain.models import Observation, ObservationEnvelope, SourceDocument
from bilibot.video_understanding.audio_track import (
    ASRTranscriptionResult,
    transcribe_audio,
)


logger = logging.getLogger("bilibot.repair.video_asr")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--account-id", default="default")
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--target-event-id", required=True)
    parser.add_argument(
        "--asr-backend",
        choices=("provider", "local"),
        default="provider",
    )
    parser.add_argument("--whisper-model-size", default="base")
    parser.add_argument("--whisper-device", default="cpu")
    parser.add_argument("--whisper-compute-type", default="int8")
    parser.add_argument(
        "--reuse-work-dir",
        help="Reuse a retained repair directory instead of downloading again.",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Keep downloaded media after a successful archive.",
    )
    return parser.parse_args()


def _load_account(raw: Mapping[str, Any], account_id: str) -> Mapping[str, Any]:
    for account in raw.get("accounts") or ():
        if isinstance(account, Mapping) and str(account.get("id") or "") == account_id:
            return account
    raise RuntimeError(f"configured account not found: {account_id}")


def _account_loader(
    raw: Mapping[str, Any], account: Mapping[str, Any], account_data_dir: Path
) -> ConfigLoader:
    merged = copy.deepcopy(dict(raw))
    merged["bilibili"] = {
        key: account.get(key, "")
        for key in (
            "sessdata",
            "bili_jct",
            "dede_user_id",
            "buvid3",
            "refresh_token",
        )
    }
    merged["data_dir"] = str(account_data_dir)
    merged.pop("accounts", None)
    loader = ConfigLoader(merged)
    if not loader.bilibili.is_authenticated:
        raise RuntimeError("Bilibili credentials are not configured for the account")
    if any(
        is_sensitive_placeholder(value)
        for value in (
            loader.bilibili.sessdata,
            loader.bilibili.bili_jct,
            loader.bilibili.buvid3,
        )
    ):
        raise RuntimeError("Bilibili credentials contain a redaction placeholder")
    return loader


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0 or not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr[-300:]}")


def _supplement_envelope(
    *,
    account_id: str,
    bvid: str,
    oid: str,
    title: str,
    target_event_id: str,
    result: ASRTranscriptionResult,
    asr_model: str,
) -> ObservationEnvelope:
    segments = [
        {
            "start": float(event.start),
            "end": float(event.end),
            "text": str(event.text),
            "source": str(getattr(event, "source", "asr") or "asr"),
        }
        for event in result.events
        if str(event.text).strip()
    ]
    full_text = "\n".join(item["text"] for item in segments)
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    observations: Sequence[Observation]
    source_type = "asr"
    if segments:
        observations = tuple(
            Observation(
                text=item["text"],
                modality="asr",
                start_ms=max(0, int(item["start"] * 1000)),
                end_ms=max(0, int(item["end"] * 1000)),
                data={"source": item["source"]},
                extractor_version="video-asr-supplement-v1",
            )
            for item in segments
        )
    else:
        source_type = "asr_status"
        observations = ()

    return ObservationEnvelope(
        idempotency_key=(
            f"video_asr_supplement:{account_id}:{bvid}:{target_event_id}:{digest}"
        ),
        account_id=account_id,
        source_type=source_type,
        event_type="video_asr_supplement",
        event_title=f"{title} (ASR supplement)",
        event_summary=("" if segments else "ASR completed with no speech detected."),
        scene="proactive_video_repair",
        importance=0.65,
        occurred_at=time.time(),
        metadata={
            "bvid": bvid,
            "oid": oid,
            "target_event_id": target_event_id,
            "relation": "updates",
            "audio_status": result.status,
            "segment_count": len(segments),
            "asr_model": asr_model,
        },
        sources=(
            SourceDocument(
                source_type=source_type,
                external_id=bvid or oid,
                full_text=full_text,
                data={
                    "status": result.status,
                    "segments": segments,
                    "target_event_id": target_event_id,
                },
                observations=observations,
            ),
        ),
    )


def _link_and_verify(memory: MemoryBrainService, supplement: Any, target_event_id: str) -> None:
    observation_evidence = []
    if supplement.observation_ids:
        observation_evidence.append(supplement.observation_ids[0])
        if supplement.observation_ids[-1] != supplement.observation_ids[0]:
            observation_evidence.append(supplement.observation_ids[-1])
    evidence_ids = [
        supplement.event_id,
        *supplement.source_ids,
        *observation_evidence,
        target_event_id,
    ]
    memory.store.upsert_links(
        supplement.event_id,
        (
            {
                "target_event_id": target_event_id,
                "relation_type": "updates",
                "weight": 1.0,
                "evidence_ids": evidence_ids,
            },
        ),
    )
    event = memory.get_event(supplement.event_id, chunks_per_event=None)
    verified = any(
        link.get("target_event_id") == target_event_id
        and link.get("relation_type") == "updates"
        for link in (event or {}).get("links", ())
    )
    if not verified:
        raise RuntimeError("supplement archive committed but updates link verification failed")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    account_id = str(args.account_id)
    account = _load_account(raw, account_id)
    data_root = Path(str(raw.get("data_dir") or "./data")).resolve()
    account_data_dir = (data_root / "accounts" / account_id).resolve()
    if data_root not in account_data_dir.parents:
        raise RuntimeError("account data directory escapes configured data root")

    app_loader = ConfigLoader(dict(raw), filepath=str(config_path))
    account_loader = _account_loader(raw, account, account_data_dir)
    router = LLMManager(app_loader)
    router.initialize()
    asr_provider = router.resolve_asr()
    if args.asr_backend == "provider":
        if (
            asr_provider is None
            or not asr_provider.model
            or not asr_provider.api_key
            or is_sensitive_placeholder(asr_provider.api_key)
        ):
            raise RuntimeError("ASR provider is not fully configured")

    memory = MemoryBrainService(
        account_id,
        account_data_dir,
        chat_provider=router.resolve_chat(),
        embedding_provider=router.resolve_embedding(),
        memory_config=account_loader.memory,
    )
    target = memory.get_event(str(args.target_event_id), chunks_per_event=0)
    if target is None:
        raise RuntimeError(f"target memory event not found: {args.target_event_id}")
    target_bvid = str((target.get("metadata") or {}).get("bvid") or "")
    if target_bvid and target_bvid != args.bvid:
        raise RuntimeError("target memory event BVID does not match --bvid")

    repair_root = (account_data_dir / "video_temp").resolve()
    if args.reuse_work_dir:
        repair_dir = Path(args.reuse_work_dir).resolve()
        if repair_root != repair_dir and repair_root not in repair_dir.parents:
            raise RuntimeError("reused repair directory escapes account video_temp")
        if not repair_dir.is_dir():
            raise RuntimeError("reused repair directory does not exist")
    else:
        repair_dir = repair_root / f"asr_repair_{args.bvid}_{uuid.uuid4().hex[:8]}"
        repair_dir.mkdir(parents=True, exist_ok=False)
    video_path = repair_dir / "source.mp4"
    audio_path = repair_dir / "audio.wav"
    bili = BilibiliAPI(account_loader)
    committed = False
    try:
        oid_value = await bili.get_video_oid_by_bvid(args.bvid)
        if not oid_value:
            raise RuntimeError(f"unable to resolve BVID: {args.bvid}")
        info = await bili.get_video_info(int(oid_value))
        if not isinstance(info, Mapping):
            raise RuntimeError("unable to load video metadata")
        cid = info.get("cid")
        if not cid and info.get("pages"):
            cid = info["pages"][0].get("cid")
        if not cid:
            raise RuntimeError("video metadata contains no CID")

        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            if not video_path.is_file() or video_path.stat().st_size == 0:
                downloaded = await bili.download_video(
                    args.bvid,
                    int(cid),
                    str(video_path.with_suffix("")),
                    quality=32,
                )
                if not downloaded or not Path(downloaded).is_file():
                    raise RuntimeError("video download failed")
                video_path = Path(downloaded)
            await asyncio.to_thread(_extract_audio, video_path, audio_path)

        if args.asr_backend == "local":
            asr_model_label = f"local:{args.whisper_model_size}"
            asr_result = await asyncio.to_thread(
                transcribe_audio,
                str(audio_path),
                whisper_model_size=args.whisper_model_size,
                whisper_device=args.whisper_device,
                whisper_compute_type=args.whisper_compute_type,
                local_whisper_enabled=True,
                max_local_whisper_workers=1,
                whisper_timeout=3600,
                return_result=True,
                raise_on_error=True,
            )
        else:
            asr_model_label = str(asr_provider.model)
            asr_result = await asyncio.to_thread(
                transcribe_audio,
                str(audio_path),
                asr_model=asr_provider.model,
                asr_api_key=asr_provider.api_key,
                asr_base_url=asr_provider.base_url,
                local_whisper_enabled=False,
                return_result=True,
                raise_on_error=True,
            )
        if not isinstance(asr_result, ASRTranscriptionResult):
            raise RuntimeError("ASR returned an unexpected result type")

        envelope = _supplement_envelope(
            account_id=account_id,
            bvid=args.bvid,
            oid=str(oid_value),
            title=str(info.get("title") or target.get("title") or args.bvid),
            target_event_id=str(args.target_event_id),
            result=asr_result,
            asr_model=asr_model_label,
        )
        archived = await memory.archive_observation_async(envelope)
        if archived.source_committed is not True:
            raise RuntimeError("supplement source commit was not confirmed")
        await asyncio.to_thread(
            _link_and_verify, memory, archived, str(args.target_event_id)
        )
        committed = True
        return {
            "event_id": archived.event_id,
            "target_event_id": str(args.target_event_id),
            "relation": "updates",
            "created": archived.created,
            "audio_status": asr_result.status,
            "asr_backend": args.asr_backend,
            "segments": len(asr_result.events),
            "characters": sum(len(event.text) for event in asr_result.events),
            "artifacts_removed": not args.keep_artifacts,
            "work_dir": str(repair_dir) if args.keep_artifacts else "",
        }
    finally:
        await bili.close()
        memory.flush()
        if committed and not args.keep_artifacts:
            shutil.rmtree(repair_dir)
        elif repair_dir.exists():
            logger.warning("repair artifacts retained: %s", repair_dir)


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        logger.error("ASR supplement repair failed: %s: %s", type(exc).__name__, exc)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
