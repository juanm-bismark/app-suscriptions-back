from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings, require_fernet_key
from app.providers.kite.client import KiteClient
from app.shared.crypto import decrypt_credentials
from app.shared.errors import DomainError, ProviderRateLimited, ProviderUnavailable
from app.subscriptions.models.routing import SimRoutingMap
from app.tenancy.models.credentials import CompanyProviderCredentials


@dataclass
class ProbeResult:
    interval: float
    attempt: int
    elapsed: float
    status: str
    code: str | None
    detail: str | None


def _parse_intervals(raw: str) -> list[float]:
    intervals = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not intervals:
        raise argparse.ArgumentTypeError("intervals must include at least one value")
    if any(interval < 0 for interval in intervals):
        raise argparse.ArgumentTypeError("intervals must be non-negative")
    return intervals


def _parse_ints(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("value must include at least one integer")
    if any(value < 1 for value in values):
        raise argparse.ArgumentTypeError("values must be >= 1")
    return values


def _parse_iccids(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("iccids must include at least one value")
    return values


def _classify_error(exc: Exception) -> tuple[str, str | None, str | None]:
    if isinstance(exc, ProviderRateLimited):
        return "rate_limited", exc.code, exc.detail
    if isinstance(exc, ProviderUnavailable):
        if exc.extra.get("retryable") is True or exc.extra.get("provider_error_code") == "SVR.1006":
            return "overloaded", exc.code, exc.detail
        return "unavailable", exc.code, exc.detail
    if isinstance(exc, DomainError):
        return "error", exc.code, exc.detail
    return "error", exc.__class__.__name__, str(exc)


async def _load_probe_inputs(
    company_id: uuid.UUID | None,
    iccid: str | None,
    *,
    iccids: Sequence[str] | None = None,
    iccid_count: int = 1,
) -> tuple[dict[str, object], list[str]]:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required")

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            credential_query = select(CompanyProviderCredentials).where(
                CompanyProviderCredentials.provider == "kite",
                CompanyProviderCredentials.active.is_(True),
            )
            if company_id is not None:
                credential_query = credential_query.where(
                    CompanyProviderCredentials.company_id == company_id
                )
            credential = (await session.execute(credential_query)).scalar_one_or_none()
            if credential is None:
                raise RuntimeError("No active Kite credentials found")

            selected_iccids = list(iccids or ([iccid] if iccid is not None else []))
            if not selected_iccids:
                routing_rows = (
                    await session.execute(
                        select(SimRoutingMap)
                        .where(
                            SimRoutingMap.provider == "kite",
                            SimRoutingMap.company_id == credential.company_id,
                        )
                        .order_by(SimRoutingMap.iccid)
                        .limit(max(iccid_count, 1))
                    )
                ).scalars().all()
                if not routing_rows:
                    raise RuntimeError("No Kite ICCID found in sim_routing_map")
                selected_iccids = [routing.iccid for routing in routing_rows]

            credentials = decrypt_credentials(
                credential.credentials_enc, require_fernet_key(settings)
            )
            credentials["company_id"] = str(credential.company_id)
            return credentials, selected_iccids
    finally:
        await engine.dispose()


async def _probe_interval(
    client: KiteClient,
    iccid: str,
    interval: float,
    attempts: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    next_start = time.monotonic()
    for attempt in range(1, attempts + 1):
        wait = max(0.0, next_start - time.monotonic())
        if wait:
            await asyncio.sleep(wait)
        start = time.monotonic()
        try:
            await client.get_subscription_detail(iccid)
            status, code, detail = "ok", None, None
        except Exception as exc:
            status, code, detail = _classify_error(exc)
        elapsed = time.monotonic() - start
        results.append(
            ProbeResult(
                interval=interval,
                attempt=attempt,
                elapsed=elapsed,
                status=status,
                code=code,
                detail=detail,
            )
        )
        next_start = start + interval
    return results


async def _call_detail(
    client: KiteClient,
    iccid: str,
    index: int,
    interval: float,
) -> ProbeResult:
    start = time.monotonic()
    try:
        await client.get_subscription_detail(iccid)
        status, code, detail = "ok", None, None
    except Exception as exc:
        status, code, detail = _classify_error(exc)
    return ProbeResult(
        interval=interval,
        attempt=index,
        elapsed=time.monotonic() - start,
        status=status,
        code=code,
        detail=detail,
    )


async def _probe_burst(
    client: KiteClient,
    iccids: Sequence[str],
    burst_size: int,
) -> list[ProbeResult]:
    selected = [iccids[index % len(iccids)] for index in range(burst_size)]
    return await asyncio.gather(
        *(
            _call_detail(client, iccid, index + 1, 0.0)
            for index, iccid in enumerate(selected)
        )
    )


def _print_result(result: ProbeResult) -> None:
    rate_per_minute = 60 / result.interval if result.interval > 0 else float("inf")
    print(
        f"interval={result.interval:.2f}s "
        f"target_rate={rate_per_minute:.1f}/min "
        f"attempt={result.attempt} "
        f"elapsed={result.elapsed:.2f}s "
        f"status={result.status} "
        f"code={result.code or '-'} "
        f"detail={result.detail or '-'}",
        flush=True,
    )


async def run_probe(
    *,
    company_id: uuid.UUID | None,
    iccid: str | None,
    intervals: Sequence[float],
    attempts: int,
    cooldown: float,
    stop_on_throttle: bool,
) -> int:
    settings = get_settings()
    settings.kite_retry_max_attempts = 1
    settings.kite_max_concurrent_requests = 1

    credentials, selected_iccids = await _load_probe_inputs(company_id, iccid)
    selected_iccid = selected_iccids[0]
    client = KiteClient(credentials)

    print(f"probe_iccid={selected_iccid}", flush=True)
    print("retry=disabled concurrency=1 interval=start-to-start", flush=True)
    for interval in intervals:
        print(
            f"\nphase interval={interval:.2f}s target_rate={60 / interval if interval else float('inf'):.1f}/min",
            flush=True,
        )
        results = await _probe_interval(client, selected_iccid, interval, attempts)
        for result in results:
            _print_result(result)
        throttled = any(result.status in {"rate_limited", "overloaded"} for result in results)
        if throttled and stop_on_throttle:
            print("\nstopped_after_throttle=true", flush=True)
            return 2
        if cooldown > 0 and interval != intervals[-1]:
            print(f"cooldown={cooldown:.1f}s", flush=True)
            await asyncio.sleep(cooldown)
    return 0


async def run_burst_probe(
    *,
    company_id: uuid.UUID | None,
    iccids: Sequence[str] | None,
    burst_sizes: Sequence[int],
    repeats: int,
    cooldown: float,
    stop_on_throttle: bool,
) -> int:
    settings = get_settings()
    settings.kite_retry_max_attempts = 1
    settings.kite_max_concurrent_requests = max(max(burst_sizes), 1)

    credentials, selected_iccids = await _load_probe_inputs(
        company_id,
        None,
        iccids=iccids,
        iccid_count=max(burst_sizes),
    )
    client = KiteClient(credentials)

    print(f"probe_iccids={','.join(selected_iccids[:max(burst_sizes)])}", flush=True)
    print("retry=disabled concurrency=uncapped-for-probe mode=burst", flush=True)
    for burst_size in burst_sizes:
        print(f"\nphase burst_size={burst_size} repeats={repeats}", flush=True)
        for repeat in range(1, max(repeats, 1) + 1):
            print(f"repeat={repeat}", flush=True)
            results = await _probe_burst(client, selected_iccids, burst_size)
            for result in results:
                _print_result(result)
            throttled = any(
                result.status in {"rate_limited", "overloaded"} for result in results
            )
            if throttled and stop_on_throttle:
                print("\nstopped_after_throttle=true", flush=True)
                return 2
            if cooldown > 0:
                print(f"cooldown={cooldown:.1f}s", flush=True)
                await asyncio.sleep(cooldown)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe Kite SOAP operational request-rate control safely."
    )
    parser.add_argument("--company-id", type=uuid.UUID, default=None)
    parser.add_argument("--mode", choices=["rate", "burst"], default="rate")
    parser.add_argument("--iccid", default=None)
    parser.add_argument("--iccids", type=_parse_iccids, default=None)
    parser.add_argument("--intervals", type=_parse_intervals, default="3,2,1.5")
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--burst-sizes", type=_parse_ints, default="1,2,3,4,5")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cooldown", type=float, default=15.0)
    parser.add_argument("--no-stop-on-throttle", action="store_true")
    args = parser.parse_args()

    if args.mode == "burst":
        code = asyncio.run(
            run_burst_probe(
                company_id=args.company_id,
                iccids=args.iccids,
                burst_sizes=args.burst_sizes,
                repeats=max(args.repeats, 1),
                cooldown=max(args.cooldown, 0.0),
                stop_on_throttle=not args.no_stop_on_throttle,
            )
        )
    else:
        code = asyncio.run(
            run_probe(
                company_id=args.company_id,
                iccid=args.iccid,
                intervals=args.intervals,
                attempts=max(args.attempts, 1),
                cooldown=max(args.cooldown, 0.0),
                stop_on_throttle=not args.no_stop_on_throttle,
            )
        )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
