"""Actualiza la x_api_key de Moabits en company_provider_credentials usando el
valor de .env (`x-api-key-moabits`).

Uso:
    python -m scripts.update_moabits_api_key             # dry-run, solo imprime
    python -m scripts.update_moabits_api_key --apply     # aplica el cambio
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.shared.crypto import decrypt_credentials, encrypt_credentials
from app.tenancy.models.credentials import CompanyProviderCredentials


def _mask(v: object) -> str:
    s = str(v)
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:2]}...{s[-6:]} (len={len(s)})"


async def main(apply: bool) -> int:
    load_dotenv()
    fernet_key = os.environ.get("FERNET_KEY")
    new_key = os.environ.get("x-api-key-moabits")
    db_url = os.environ.get("DATABASE_URL")
    if not (fernet_key and new_key and db_url):
        print(
            "ERROR: missing FERNET_KEY, x-api-key-moabits or DATABASE_URL in .env",
            file=sys.stderr,
        )
        return 2

    engine = create_async_engine(db_url)
    async with AsyncSession(engine) as session:
        rows = (
            (
                await session.execute(
                    select(CompanyProviderCredentials).where(
                        CompanyProviderCredentials.provider == "moabits",
                        CompanyProviderCredentials.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        if len(rows) == 0:
            print("No active Moabits credential rows found.")
            return 1
        if len(rows) > 1:
            print(
                f"Found {len(rows)} active rows — refusing to update without --company-id filter."
            )
            for r in rows:
                print(f"  - id={r.id} company_id={r.company_id}")
            return 1

        row = rows[0]
        creds = decrypt_credentials(row.credentials_enc, fernet_key)
        print(f"Row: id={row.id} company_id={row.company_id}")
        print(f"Stored credential keys: {sorted(creds.keys())}")
        for k, v in creds.items():
            if k in ("x_api_key", "api_key", "apiKey", "token"):
                print(f"  {k} = {_mask(v)}")
            else:
                print(f"  {k} = {v!r}")

        if "x_api_key" not in creds:
            print(
                "ERROR: stored credentials do NOT contain `x_api_key`. "
                "The adapter expects exactly this key (see app/providers/moabits/adapter.py:94). "
                "Aborting — run a corrective PATCH or re-create the credential.",
                file=sys.stderr,
            )
            return 3

        old_key = creds.get("x_api_key", "")
        if old_key == new_key:
            print("Stored x_api_key already matches .env value. Nothing to do.")
            return 0

        print(f"Old x_api_key: {_mask(old_key)}")
        print(f"New x_api_key: {_mask(new_key)}")

        if not apply:
            print("\nDry-run only. Re-run with --apply to persist the change.")
            return 0

        creds["x_api_key"] = new_key
        row.credentials_enc = encrypt_credentials(creds, fernet_key)
        await session.commit()
        print("UPDATED.")
        return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apply",
        action="store_true",
        help="Persist the update (default: dry-run)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply)))
