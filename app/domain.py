"""幂等入库 / 回滚领域层。

所有闸口都在这一层强制，HTTP 层与后台线程共用同一套逻辑：
- 幂等策略当日钉死，处置漂移直接拒绝；
- 空钥条、同流争用、本人回滚、回滚未收尾再入库均在服务端拦截；
- 入库去重与回滚在后台工作线程做齐，结果以事件流留痕。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime

from app.config import Config
from app.db import connect, get_flow

STRATEGIES = {
    "skip_count": "遇撞跳过计数",
    "rollback_batch": "遇撞整批撤回",
}
EVENT_TITLES = {
    "accepted": "入库成功",
    "skipped": "幂等撞钥跳过",
    "auto_rolled_back": "撞钥整批回滚",
    "manual_rolled_back": "人工回滚",
    "contention": "争用入库败绩",
    "empty_key": "空钥条败绩",
    "policy_drift": "策略漂移拦截",
    "recovered": "重启中断恢复",
}

# 同流在途批次 / 在途回滚的进程内闸（SQLite 部分唯一索引兜底幂等本身）
_inflight_flows: set[int] = set()
_state_lock = threading.RLock()
# 工作线程收尾信号：测试可确定性等待，不靠 sleep 猜时序
_worker_done: dict[int, threading.Event] = {}
# 每个数据库只在进程首次启动时清扫一次孤儿（测试客户端每请求触发启动事件，需防重复清扫）
_recovered_dbs: set[str] = set()
_recover_lock = threading.Lock()


def wait_worker(batch_id: int, timeout: float = 5.0) -> bool:
    event = _worker_done.get(batch_id)
    if event is None:
        return False
    return event.wait(timeout)


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _add_event(conn, batch_id: int, flow_id: int, event_type: str,
               message: str, idem_key: str = "", operator: str = "") -> None:
    conn.execute(
        """INSERT INTO land_event
           (batch_id, flow_id, event_type, idem_key, message, operator, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (batch_id, flow_id, event_type, idem_key, message, operator, _now()),
    )


def _worker_connect():
    # autocommit：工作线程自行用 BEGIN IMMEDIATE 划事务
    conn = connect()
    conn.isolation_level = None
    return conn


# ---------- 幂次策略：当日钉死一种，不许更换 ----------

def get_today_policy(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM idempotency_policy WHERE policy_date=?", (_today(),)
    ).fetchone()
    return dict(row) if row else None


def pin_policy(conn, strategy: str, operator: str) -> dict:
    if strategy not in STRATEGIES:
        raise DomainError("未知幂次策略")
    existing = get_today_policy(conn)
    if existing and existing["strategy"] != strategy:
        raise DomainError(
            f"今日幂次策略已钉死为「{existing['title']}」，当天不许更换",
            status=409,
        )
    if existing:
        return existing
    row = {
        "policy_date": _today(),
        "strategy": strategy,
        "title": STRATEGIES[strategy],
        "pinned_by": operator,
        "pinned_at": _now(),
    }
    conn.execute(
        """INSERT INTO idempotency_policy
           (policy_date, strategy, title, pinned_by, pinned_at)
           VALUES (:policy_date,:strategy,:title,:pinned_by,:pinned_at)""",
        row,
    )
    return row


# ---------- 入库列表 / 批次 / 回滚详情 ----------

def list_batches(conn, flow_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM landing_batch"
    params: tuple = ()
    if flow_id is not None:
        sql += " WHERE flow_id=?"
        params = (flow_id,)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, params)]


def get_batch(conn, batch_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM landing_batch WHERE id=?", (batch_id,)
    ).fetchone()
    return dict(row) if row else None


def list_records(conn, batch_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, batch_id, flow_id, idem_key, state, created_at "
        "FROM landing_record WHERE batch_id=? ORDER BY id",
        (batch_id,),
    )]


def list_events(conn, batch_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM land_event WHERE batch_id=? ORDER BY id", (batch_id,)
    )]


def list_rollback_orders(conn, batch_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM rollback_order WHERE batch_id=? ORDER BY id", (batch_id,)
    )]


def rollback_detail(conn, rollback_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM rollback_order WHERE id=?", (rollback_id,)
    ).fetchone()
    if not row:
        return None
    order = dict(row)
    order["events"] = list_events(conn, order["batch_id"])
    order["batch"] = get_batch(conn, order["batch_id"])
    order["records"] = list_records(conn, order["batch_id"])
    return order


def batch_detail(conn, batch_id: int) -> dict | None:
    batch = get_batch(conn, batch_id)
    if not batch:
        return None
    batch["records"] = list_records(conn, batch_id)
    batch["events"] = list_events(conn, batch_id)
    batch["rollback_orders"] = list_rollback_orders(conn, batch_id)
    return batch


def is_flow_busy(conn, flow_id: int) -> bool:
    """回滚尚未收尾或有在途批次时，禁止再次开入库。"""
    with _state_lock:
        if flow_id in _inflight_flows:
            return True
    row = conn.execute(
        """SELECT COUNT(*) c FROM landing_batch
           WHERE flow_id=? AND status IN ('处理中','回滚中')""",
        (flow_id,),
    ).fetchone()
    return row["c"] > 0


def recover_orphans(conn) -> None:
    """服务重启后清扫宕机时未收尾的在途批次/回滚单，避免永久占用争用闸。

    每个物理数据库只清扫一次；测试客户端会为每个请求重放启动事件，
    若不守卫会把仍在后台处理的在途批次误判成孤儿。
    """
    from app.config import Config
    db_key = Config.database()
    with _recover_lock:
        if db_key in _recovered_dbs:
            return
        _recovered_dbs.add(db_key)

    stuck_batches = [dict(r) for r in conn.execute(
        "SELECT * FROM landing_batch WHERE status='处理中'"
    )]
    for b in stuck_batches:
        conn.execute(
            "UPDATE landing_record SET state='failed' "
            "WHERE batch_id=? AND state='pending'", (b["id"],)
        )
        conn.execute(
            "UPDATE landing_batch SET status='处理失败', note=?, updated_at=? WHERE id=?",
            ("服务重启，后台入库中断，按失败处理", _now(), b["id"]),
        )
        _add_event(conn, b["id"], b["flow_id"], "recovered",
                   "服务重启导致在途入库中断，批次按失败处理，可重新开入库",
                   operator=b["operator"])
    stuck_rollbacks = [dict(r) for r in conn.execute(
        "SELECT * FROM rollback_order WHERE status='回滚中'"
    )]
    for o in stuck_rollbacks:
        conn.execute(
            "UPDATE rollback_order SET status='回滚失败', body=body||? WHERE id=?",
            (f"\n服务重启导致回滚中断（{_now()}），可重新发起回滚", o["id"]),
        )
        conn.execute(
            "UPDATE landing_batch SET status='回滚失败', note=?, updated_at=? WHERE id=?",
            ("服务重启，后台回滚中断", _now(), o["batch_id"]),
        )
        _add_event(conn, o["batch_id"], o["flow_id"], "recovered",
                   "服务重启导致在途回滚中断，可重新发起回滚",
                   operator=o["requestor"])
    if stuck_batches or stuck_rollbacks:
        conn.commit()


def _persist_rejection(conn, flow_id: int, strategy: str, status: str,
                       note: str, event_type: str, message: str,
                       operator: str, key_count: int) -> int:
    """败绩也要留痕：先落库提交，再由调用方抛错。"""
    ghost_id = conn.execute(
        """INSERT INTO landing_batch
           (flow_id, strategy, strategy_title, status, operator,
            requested_count, note, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (flow_id, strategy, STRATEGIES.get(strategy, strategy), status,
         operator, key_count, note, _now(), _now()),
    ).lastrowid
    _add_event(conn, ghost_id, flow_id, event_type, message, operator=operator)
    conn.commit()
    return ghost_id


# ---------- 入库：开批（同步闸口） + 后台做齐 ----------

def create_landing(conn, flow_id: int, strategy: str, operator: str,
                   raw_keys: list[str]) -> dict:
    flow = get_flow(conn, flow_id)
    if not flow:
        raise DomainError("流不存在", status=404)
    if strategy not in STRATEGIES:
        raise DomainError("未知幂次策略")

    keys = [str(k).strip() for k in raw_keys]

    # 闸 1：空钥匙败绩——整批不允许有空钥条
    if not keys or any(k == "" for k in keys):
        _persist_rejection(
            conn, flow_id, strategy, "空钥败绩", "钥条空缺禁止入库",
            "empty_key", f"提交 {len(raw_keys)} 条，含空钥条，整批禁止入库",
            operator, len(raw_keys),
        )
        raise DomainError("钥条空缺禁止入库", status=400)

    # 闸 2：幂次策略当日钉死；处置与钉死策略对不上即“处置漂了”，拦
    pinned = get_today_policy(conn)
    if pinned is None:
        pin_policy(conn, strategy, operator)
        pinned = get_today_policy(conn)
    elif pinned["strategy"] != strategy:
        _persist_rejection(
            conn, flow_id, strategy, "策略漂移拦截",
            f"请求策略与今日钉死策略「{pinned['title']}」不符",
            "policy_drift",
            f"处置漂了：请求「{STRATEGIES[strategy]}」，今日钉死「{pinned['title']}」",
            operator, len(keys),
        )
        raise DomainError(
            f"幂次处置漂了：今日已钉死「{pinned['title']}」，不得按"
            f"「{STRATEGIES[strategy]}」入库",
            status=400,
        )

    # 闸 3：争用——同流在途（入库处理中 / 回滚中）后到者记争用败绩。
    # 检查、开批、占 inflight 在同一进程锁内原子完成。
    with _state_lock:
        if flow_id in _inflight_flows or is_flow_busy(conn, flow_id):
            _persist_rejection(
                conn, flow_id, strategy, "争用败绩",
                "同流已有在途入库或回滚",
                "contention", "双方争抢入库，后到请求记争用败绩",
                operator, len(keys),
            )
            raise DomainError("同流入库正在处理，后到请求记争用败绩",
                              status=409)

        now = _now()
        batch_id = conn.execute(
            """INSERT INTO landing_batch
               (flow_id, strategy, strategy_title, status, operator,
                requested_count, note, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (flow_id, strategy, pinned["title"], "处理中", operator,
             len(keys), "后台处理中", now, now),
        ).lastrowid
        # pending 钥条随请求事务落库，后台线程直接可见
        conn.executemany(
            """INSERT INTO landing_record
               (batch_id, flow_id, idem_key, state, created_at)
               VALUES (?,?,?,'pending',?)""",
            [(batch_id, flow_id, k, now) for k in keys],
        )
        _inflight_flows.add(flow_id)

    return get_batch(conn, batch_id)


def start_landing_worker(batch_id: int) -> None:
    _worker_done[batch_id] = threading.Event()
    threading.Thread(
        target=_process_landing, args=(batch_id,),
        name=f"lander-{batch_id}", daemon=True,
    ).start()


def _process_landing(batch_id: int) -> None:
    """后台工作线程：幂等去重做齐，撞钥按钉死策略跳过计数或整批撤回。"""
    flow_id = None
    if Config.LANDER_DELAY and Config.LANDER_DELAY > 0:
        time.sleep(Config.LANDER_DELAY)
    conn = _worker_connect()
    try:
        batch = get_batch(conn, batch_id)
        if not batch:
            return
        flow_id = batch["flow_id"]
        strategy = batch["strategy"]
        operator = batch["operator"]
        # 等请求事务把 pending 钥条落盘（测试客户端下线程可能早于提交可见）
        pending = []
        for _ in range(100):
            pending = [dict(r) for r in conn.execute(
                "SELECT * FROM landing_record WHERE batch_id=? AND state='pending' "
                "ORDER BY id", (batch_id,)
            )]
            if pending:
                break
            time.sleep(0.02)
        if not pending:
            raise RuntimeError("批次钥条未落盘，无法做幂等去重")
        conn.execute("BEGIN IMMEDIATE")
        success = 0
        skipped = 0
        collision = False
        for rec in pending:
            key = rec["idem_key"]
            # 先探测同流是否已有活跃钥条（其他批次入库或本批更早的重复键）
            clash = conn.execute(
                "SELECT 1 FROM landing_record "
                "WHERE flow_id=? AND idem_key=? AND state='active' LIMIT 1",
                (flow_id, key),
            ).fetchone()
            if clash:
                # 幂等撞钥
                collision = True
                if strategy == "skip_count":
                    skipped += 1
                    conn.execute(
                        "UPDATE landing_record SET state='skipped' WHERE id=?",
                        (rec["id"],),
                    )
                    _add_event(conn, batch_id, flow_id, "skipped",
                               f"钥条 {key} 幂等撞钥，跳过并计数",
                               idem_key=key, operator=operator)
                    continue
                # rollback_batch：撞钥即整批败绩并撤回到入库前
                break
            # 无撞钥：pending 行直接转 active（不再另插新行，避免自撞唯一索引）
            conn.execute(
                "UPDATE landing_record SET state='active' WHERE id=?",
                (rec["id"],),
            )
            success += 1
            _add_event(conn, batch_id, flow_id, "accepted",
                       f"钥条 {key} 入库成功",
                       idem_key=key, operator=operator)

        if strategy == "rollback_batch" and collision:
            cur = conn.execute(
                "UPDATE landing_record SET state='auto_rolled_back' "
                "WHERE batch_id=? AND state IN ('active','pending')",
                (batch_id,),
            )
            rolled = cur.rowcount
            _add_event(conn, batch_id, flow_id, "auto_rolled_back",
                       f"撞钥触发整批撤回，{rolled} 条已撤回到入库前",
                       operator=operator)
            body = (
                f"【回滚单】批次 #{batch_id} 策略「{batch['strategy_title']}」"
                f"撞钥，整批撤回到入库前。\n回滚缘故：幂等撞钥触发整批撤回"
                f"（系统自行撤回）\n入库发起人：{operator}\n回滚发起人：system（自动）"
                f"\n开单时间：{_now()}"
            )
            conn.execute(
                """INSERT INTO rollback_order
                   (batch_id, flow_id, reason, requestor, status, body,
                    created_at, finished_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (batch_id, flow_id, "幂等撞钥触发整批撤回（系统自行撤回）",
                 "system", "已完成", body, _now(), _now()),
            )
            conn.execute(
                """UPDATE landing_batch
                   SET status='整批已回滚', success_count=0, skip_count=0,
                       rollback_count=rollback_count+1, note=?, updated_at=?
                   WHERE id=?""",
                ("幂等撞钥，整批败绩自行撤回", _now(), batch_id),
            )
            conn.execute(
                "UPDATE flows SET status='已抽取待落库', landed=0 WHERE id=?",
                (flow_id,),
            )
        else:
            active = conn.execute(
                "SELECT COUNT(*) c FROM landing_record "
                "WHERE flow_id=? AND state='active'", (flow_id,)
            ).fetchone()["c"]
            if strategy == "skip_count":
                if skipped and success:
                    status = "部分跳过"
                elif skipped and not success:
                    status = "全部撞钥跳过"
                else:
                    status = "已入库"
                note = f"成功 {success} 条，撞钥跳过 {skipped} 条"
            else:
                status = "已入库"
                note = f"成功 {success} 条"
            conn.execute(
                """UPDATE landing_batch
                   SET status=?, success_count=?, skip_count=?, note=?, updated_at=?
                   WHERE id=?""",
                (status, success, skipped, note, _now(), batch_id),
            )
            conn.execute(
                "UPDATE flows SET status='已落库', landed=? WHERE id=?",
                (active, flow_id),
            )
        conn.commit()
    except Exception as exc:  # 后台失败也要留痕、释放闸口
        conn.rollback()
        try:
            conn.execute(
                "UPDATE landing_batch SET status='处理失败', note=?, updated_at=? "
                "WHERE id=?",
                (f"后台处理异常：{exc}", _now(), batch_id),
            )
            conn.commit()
        except sqlite3.Error:
            pass
    finally:
        with _state_lock:
            if flow_id is not None:
                _inflight_flows.discard(flow_id)
        conn.close()
        _worker_done.get(batch_id, threading.Event()).set()


# ---------- 回滚单：缘故必填、本人不得回滚、在途禁回滚 ----------

def create_rollback(conn, batch_id: int, requestor: str, reason: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("回滚必须写明缘故")
    batch = get_batch(conn, batch_id)
    if not batch:
        raise DomainError("批次不存在", status=404)
    if batch["operator"] == requestor:
        raise DomainError("不可由刚才点入库的人亲自发起回滚", status=403)
    if batch["status"] in ("处理中", "回滚中"):
        raise DomainError("入库尚未收尾，不得发起回滚", status=409)
    active = conn.execute(
        "SELECT COUNT(*) c FROM landing_record WHERE batch_id=? AND state='active'",
        (batch_id,),
    ).fetchone()["c"]
    if active == 0:
        raise DomainError("该批次没有可回滚的活跃钥条")

    body = (
        f"【回滚单】批次 #{batch_id}（{batch['strategy_title']}）\n"
        f"回滚缘故：{reason}\n"
        f"入库发起人：{batch['operator']}\n"
        f"回滚发起人：{requestor}\n"
        f"开单时间：{_now()}"
    )
    rollback_id = conn.execute(
        """INSERT INTO rollback_order
           (batch_id, flow_id, reason, requestor, status, body,
            created_at, finished_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (batch_id, batch["flow_id"], reason, requestor, "回滚中", body,
         _now(), ""),
    ).lastrowid
    conn.execute(
        "UPDATE landing_batch SET status='回滚中', updated_at=? WHERE id=?",
        (_now(), batch_id),
    )
    with _state_lock:
        _inflight_flows.add(batch["flow_id"])
    return dict(conn.execute(
        "SELECT * FROM rollback_order WHERE id=?", (rollback_id,)
    ).fetchone())


def start_rollback_worker(rollback_id: int) -> None:
    _worker_done[rollback_id] = threading.Event()
    threading.Thread(
        target=_process_rollback, args=(rollback_id,),
        name=f"rollback-{rollback_id}", daemon=True,
    ).start()


def _process_rollback(rollback_id: int) -> None:
    flow_id = None
    batch_id = None
    if Config.ROLLBACK_DELAY and Config.ROLLBACK_DELAY > 0:
        time.sleep(Config.ROLLBACK_DELAY)
    conn = _worker_connect()
    try:
        order = conn.execute(
            "SELECT * FROM rollback_order WHERE id=?", (rollback_id,)
        ).fetchone()
        if not order:
            return
        order = dict(order)
        batch_id = order["batch_id"]
        flow_id = order["flow_id"]
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE landing_record SET state='rolled_back' "
            "WHERE batch_id=? AND state='active'", (batch_id,)
        )
        rolled = cur.rowcount
        _add_event(conn, batch_id, flow_id, "manual_rolled_back",
                   f"人工回滚 {rolled} 条活跃钥条；缘故：{order['reason']}",
                   operator=order["requestor"])
        active = conn.execute(
            "SELECT COUNT(*) c FROM landing_record "
            "WHERE flow_id=? AND state='active'", (flow_id,)
        ).fetchone()["c"]
        conn.execute(
            """UPDATE landing_batch
               SET status='已回滚', rollback_count=rollback_count+1,
                   note=?, updated_at=? WHERE id=?""",
            (f"已人工回滚，缘故：{order['reason']}", _now(), batch_id),
        )
        conn.execute(
            "UPDATE flows SET landed=?, status=? WHERE id=?",
            (active, "已抽取待落库" if active == 0 else "已落库", flow_id),
        )
        finished = _now()
        conn.execute(
            "UPDATE rollback_order SET status='已完成', finished_at=? WHERE id=?",
            (finished, rollback_id),
        )
        conn.execute(
            "UPDATE rollback_order SET body=? WHERE id=?",
            (order["body"] + f"\n收尾时间：{finished}\n回滚钥条数：{rolled}",
             rollback_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute(
                "UPDATE rollback_order SET status='回滚失败', "
                "body=body||? WHERE id=?",
                (f"\n回滚异常：{exc}", rollback_id),
            )
            if batch_id is not None:
                conn.execute(
                    "UPDATE landing_batch SET status='回滚失败', updated_at=? "
                    "WHERE id=?", (_now(), batch_id),
                )
            conn.commit()
        except sqlite3.Error:
            pass
    finally:
        with _state_lock:
            if flow_id is not None:
                _inflight_flows.discard(flow_id)
        conn.close()
        _worker_done.get(rollback_id, threading.Event()).set()
