"""
對 .env 的 DATABASE_URL（AWS RDS）執行 / 預演 / 檢查 migration。

不依賴 psql：直接用後端同一套連線設定（app.backend.database.postgresql._parse_dsn，
會處理 postgresql+asyncpg:// 前綴與 SSL）。

用法（專案根目錄，用 venv 的 python）：
  ./venv/bin/python .claude/skills/db-migrate/run_migration.py --target
  ./venv/bin/python .claude/skills/db-migrate/run_migration.py --status
  ./venv/bin/python .claude/skills/db-migrate/run_migration.py --dry-run V012 V013
  ./venv/bin/python .claude/skills/db-migrate/run_migration.py --apply --confirm-db Insight V012 V013

- --dry-run：在交易裡執行後 ROLLBACK，確認 SQL 能跑、不留下任何變更。
- --apply：必須帶 --confirm-db <資料庫名稱>，且要與連線目標相同，避免連錯庫。
- 每個檔案各自一個交易（失敗整份回滾）；含 CONCURRENTLY 的檔案無法放進交易，會直接執行並提示。
- lock_timeout 5 秒：拿不到表鎖就放棄，不會卡住正式站的請求。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "app" / "backend" / "database" / "migrations"
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import asyncpg  # noqa: E402

from app.backend.database.postgresql import _parse_dsn  # noqa: E402

LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "120s"


# ── 目標資料庫 ───────────────────────────────────────────────

def describe_target() -> dict:
    dsn, ssl_ctx = _parse_dsn()
    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
        "user": parsed.username,
        "ssl": ssl_ctx is not None,
        "is_rds": bool(parsed.hostname and parsed.hostname.endswith(".rds.amazonaws.com")),
    }


async def connect() -> asyncpg.Connection:
    dsn, ssl_ctx = _parse_dsn()
    kwargs = {"dsn": dsn, "timeout": 15}
    if ssl_ctx is not None:
        kwargs["ssl"] = ssl_ctx
    return await asyncpg.connect(**kwargs)


# ── 找檔案 ───────────────────────────────────────────────────

def resolve_files(names: list[str]) -> list[Path]:
    """接受 V012、V012__chats_source.sql 或完整路徑；依編號排序。"""
    all_files = sorted(MIGRATIONS.glob("V*.sql"))
    picked: list[Path] = []
    for name in names:
        p = Path(name)
        if p.is_file():
            picked.append(p.resolve())
            continue
        matches = [f for f in all_files if f.name == name or f.name.startswith(f"{name}__")]
        if len(matches) != 1:
            raise SystemExit(f"找不到唯一對應的 migration：{name}（候選：{[m.name for m in matches]}）")
        picked.append(matches[0])
    return sorted(set(picked), key=lambda f: f.name)


# ── 從 SQL 抓出會建立的物件，用來判斷是否已套用、以及套用後驗證 ─────────

_RE = {
    "table": re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I),
    "column": re.compile(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I
    ),
    "index": re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I),
    "constraint": re.compile(r"ADD\s+CONSTRAINT\s+(\w+)", re.I),
}


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def expected_objects(sql: str) -> list[tuple[str, tuple[str, ...]]]:
    body = _strip_comments(sql)
    objs: list[tuple[str, tuple[str, ...]]] = []
    for kind, rx in _RE.items():
        for m in rx.finditer(body):
            objs.append((kind, tuple(g.split(".")[-1] for g in m.groups())))
    return list(dict.fromkeys(objs))


async def object_exists(conn: asyncpg.Connection, kind: str, names: tuple[str, ...]) -> bool:
    if kind == "table":
        return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", names[0])
    if kind == "column":
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name=$1 AND column_name=$2)",
            names[0], names[1],
        )
    if kind == "index":
        return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname=$1)", names[0])
    if kind == "constraint":
        return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=$1)", names[0])
    return False


_DROP_RE = {
    "table": re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)", re.I),
    "index": re.compile(r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([\w.]+)", re.I),
    "constraint": re.compile(r"DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)", re.I),
    "column": re.compile(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)\s+DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(\w+)", re.I
    ),
}


def dropped_later(path: Path) -> dict[tuple[str, tuple[str, ...]], str]:
    """之後編號的 migration 刻意 DROP 掉的物件 → 哪個檔案移除的（避免誤判成「部分套用」）。"""
    out: dict[tuple[str, tuple[str, ...]], str] = {}
    for later in sorted(MIGRATIONS.glob("V*.sql")):
        if later.name <= path.name:
            continue
        body = _strip_comments(later.read_text(encoding="utf-8"))
        for kind, rx in _DROP_RE.items():
            for m in rx.finditer(body):
                out.setdefault((kind, tuple(g.split(".")[-1] for g in m.groups())), later.name)
    return out


async def migration_status(conn: asyncpg.Connection, path: Path) -> tuple[str, list[str]]:
    objs = expected_objects(path.read_text(encoding="utf-8"))
    if not objs:
        return "無法判斷（沒有可偵測的表／欄位／索引，例如種子資料）", []
    removed = dropped_later(path)
    details, present = [], 0
    for kind, names in objs:
        ok = await object_exists(conn, kind, names)
        if not ok and (kind, names) in removed:
            present += 1
            details.append(f"– {kind} {'.'.join(names)}（已由 {removed[(kind, names)]} 移除）")
            continue
        present += ok
        details.append(f"{'✓' if ok else '✗'} {kind} {'.'.join(names)}")
    if present == len(objs):
        return "已套用", details
    if present == 0:
        return "未套用", details
    return "部分套用（請人工確認）", details


# ── 執行 ─────────────────────────────────────────────────────

async def run_file(conn: asyncpg.Connection, path: Path, apply: bool) -> None:
    sql = path.read_text(encoding="utf-8")
    if re.search(r"\bCONCURRENTLY\b", _strip_comments(sql), re.I):
        if not apply:
            print(f"  ⚠️ {path.name} 含 CONCURRENTLY，無法在交易中預演；略過預演，請人工檢查 SQL。")
            return
        print(f"  ⚠️ {path.name} 含 CONCURRENTLY，不使用交易直接執行。")
        await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        await conn.execute(sql)
        return

    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        await conn.execute(sql)
    except Exception:
        await tx.rollback()
        raise
    if apply:
        await tx.commit()
    else:
        await tx.rollback()


async def describe_objects(conn: asyncpg.Connection, path: Path) -> None:
    tables = {names[0] for kind, names in expected_objects(path.read_text(encoding="utf-8")) if kind in ("table", "column")}
    for table in sorted(tables):
        cols = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name=$1 ORDER BY ordinal_position",
            table,
        )
        idx = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename=$1 ORDER BY indexname", table)
        print(f"  表 {table}：{len(cols)} 個欄位")
        for c in cols:
            null = "" if c["is_nullable"] == "YES" else " NOT NULL"
            default = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
            print(f"    - {c['column_name']} {c['data_type']}{null}{default}")
        if idx:
            print(f"    索引：{', '.join(r['indexname'] for r in idx)}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--target", action="store_true", help="只顯示連線目標（不連線）")
    mode.add_argument("--status", action="store_true", help="檢查每個 migration 是否已套用")
    mode.add_argument("--dry-run", action="store_true", help="交易內執行後 ROLLBACK")
    mode.add_argument("--apply", action="store_true", help="實際套用（需 --confirm-db）")
    ap.add_argument("--confirm-db", help="--apply 時必填，須等於目標資料庫名稱")
    ap.add_argument("files", nargs="*", help="V012、V012__xxx.sql 或路徑；--status 不帶則檢查全部")
    args = ap.parse_args()

    target = describe_target()
    print(
        f"目標：{target['user']}@{target['host']}:{target['port']}/{target['database']}"
        f"（SSL {'開' if target['ssl'] else '關'}{'，AWS RDS' if target['is_rds'] else ''}）"
    )
    if args.target:
        return 0

    if args.apply and args.confirm_db != target["database"]:
        print(f"拒絕執行：--confirm-db 必須等於目標資料庫「{target['database']}」。")
        return 2
    if (args.dry_run or args.apply) and not args.files:
        print("請指定要執行的 migration，例如 V012 V013。")
        return 2

    files = resolve_files(args.files) if args.files else sorted(MIGRATIONS.glob("V*.sql"))
    conn = await connect()
    try:
        print(f"PostgreSQL：{await conn.fetchval('SHOW server_version')}")
        if args.status:
            for f in files:
                state, details = await migration_status(conn, f)
                print(f"\n{f.name}：{state}")
                for d in details:
                    print(f"  {d}")
            return 0

        label = "套用" if args.apply else "預演"
        for f in files:
            print(f"\n▶ {label} {f.name}")
            try:
                await run_file(conn, f, apply=args.apply)
            except asyncpg.exceptions.LockNotAvailableError:
                print(f"  ✗ 拿不到表鎖（等待超過 {LOCK_TIMEOUT}），已回滾。請在離峰時段再試。")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ 失敗，已回滾：{type(exc).__name__}: {exc}")
                return 1
            print(f"  ✓ {label}成功{'（已回滾，資料庫未變更）' if not args.apply else ''}")
            if args.apply:
                state, details = await migration_status(conn, f)
                print(f"  驗證：{state}")
                for d in details:
                    print(f"    {d}")
                await describe_objects(conn, f)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
