import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app.config import Config

SEEDS = [
    ("目录扫描流", "scan_flow", "待抽取", "https://mock.local/scan"),
    ("列表翻页流", "page_flow", "待抽取", "https://mock.local/list"),
    ("附件下载流", "file_flow", "待抽取", "file://drops/pack"),
    ("变更监听流", "watch_flow", "待抽取", "https://mock.local/watch"),
]

def connect(db_path=None):
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def get_db(db_path=None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path=None):
    with get_db(db_path) as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS flows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              landed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS idempotency_policy (
              policy_date TEXT PRIMARY KEY,
              strategy TEXT NOT NULL,
              title TEXT NOT NULL,
              pinned_by TEXT NOT NULL,
              pinned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS landing_batch (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              flow_id INTEGER NOT NULL,
              strategy TEXT NOT NULL,
              strategy_title TEXT NOT NULL,
              status TEXT NOT NULL,
              operator TEXT NOT NULL,
              requested_count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0,
              skip_count INTEGER NOT NULL DEFAULT 0,
              rollback_count INTEGER NOT NULL DEFAULT 0,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS landing_record (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              flow_id INTEGER NOT NULL,
              idem_key TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            -- 幂等闸：同一流同一钥条只允许一条活跃记录；回滚后钥条释放可再入库
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_idem_key
              ON landing_record(flow_id, idem_key) WHERE state = 'active';

            CREATE TABLE IF NOT EXISTS rollback_order (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              flow_id INTEGER NOT NULL,
              reason TEXT NOT NULL,
              requestor TEXT NOT NULL,
              status TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL,
              finished_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS land_event (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              flow_id INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              idem_key TEXT NOT NULL DEFAULT '',
              message TEXT NOT NULL,
              operator TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
        ''')
        if conn.execute("SELECT COUNT(*) c FROM flows").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO flows (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEEDS,
            )

def list_flows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed FROM flows ORDER BY id"
    )]

def get_flow(conn, flow_id: int):
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, landed FROM flows WHERE id=?",
        (flow_id,),
    ).fetchone()
    return dict(row) if row else None

def run_extract(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["status"] in ("待抽取", "抽取失败"):
        n = 15 + (flow_id * 4) % 10
        conn.execute(
            "UPDATE flows SET status=?, extracted=?, landed=0 WHERE id=?",
            ("已抽取待落库", n, flow_id),
        )
    return get_flow(conn, flow_id)

def run_land(conn, flow_id: int):
    item = get_flow(conn, flow_id)
    if not item:
        return None
    if item["status"] == "已抽取待落库":
        conn.execute(
            "UPDATE flows SET status=?, landed=? WHERE id=?",
            ("已落库", item["extracted"], flow_id),
        )
    return get_flow(conn, flow_id)
