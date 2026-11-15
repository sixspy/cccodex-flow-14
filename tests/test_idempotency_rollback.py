from tests.conftest import wait_status, wait_rollback


def _post_landing(client, auth, flow_id, strategy, keys, operator="admin"):
    return client.post(
        f"/api/flows/{flow_id}/landing",
        headers=auth,
        json={"strategy": strategy, "keys": keys, "operator": operator},
    )


# ---------- 幂次策略：当日钉死，不许更换；处置漂移拦截 ----------

def test_policy_pins_and_rejects_drift(client, auth):
    _req, resp = client.post("/api/policy", headers=auth,
                             json={"strategy": "skip_count", "operator": "admin"})
    assert resp.status == 200 and resp.json["title"] == "遇撞跳过计数"
    # 重复钉同一种：幂等返回，不报错
    _req, resp2 = client.post("/api/policy", headers=auth,
                              json={"strategy": "skip_count", "operator": "admin"})
    assert resp2.status == 200
    # 想换另一种：当天不许更换
    _req, resp3 = client.post("/api/policy", headers=auth,
                              json={"strategy": "rollback_batch", "operator": "admin"})
    assert resp3.status == 409 and "不许更换" in resp3.json["detail"]

    # 处置漂了：按未钉死的策略点入库 → 400 拦截，并留策略漂移批次
    _req, land = _post_landing(client, auth, 1, "rollback_batch", ["k1"])
    assert land.status == 400 and "漂" in land.json["detail"]
    _req, batches = client.get("/api/batches", headers=auth)
    drift = [b for b in batches.json["items"] if b["status"] == "策略漂移拦截"]
    assert drift and drift[0]["note"].startswith("请求策略与今日钉死策略")


def test_first_landing_auto_pins_policy(client, auth):
    _req, land = _post_landing(client, auth, 1, "skip_count", ["a", "b"])
    assert land.status == 200
    _req, pol = client.get("/api/policy", headers=auth)
    assert pol.json["today"]["strategy"] == "skip_count"


# ---------- 空钥条败绩 ----------

def test_empty_key_batch_rejected(client, auth):
    _req, r1 = _post_landing(client, auth, 1, "skip_count", [])
    assert r1.status == 400 and "空缺" in r1.json["detail"]
    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["ok", "  ", ""])
    assert r2.status == 400
    _req, batches = client.get("/api/batches", headers=auth)
    ghosts = [b for b in batches.json["items"] if b["status"] == "空钥败绩"]
    assert len(ghosts) == 2  # 败绩留痕


# ---------- 幂等核对：撞钥跳过并计数 ----------

def test_skip_count_strategy_counts_skips(client, auth):
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["k1", "k2", "k3"])
    b1 = wait_status(client, auth, r1.json["id"], {"已入库"})
    assert b1["success_count"] == 3 and b1["skip_count"] == 0

    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["k2", "k3", "k4"])
    assert r2.status == 200
    b2 = wait_status(client, auth, r2.json["id"], {"部分跳过"})
    assert b2["success_count"] == 1 and b2["skip_count"] == 2

    detail = client.get(f"/api/batches/{b2['id']}", headers=auth)[1].json
    skipped = [e for e in detail["events"] if e["event_type"] == "skipped"]
    assert len(skipped) == 2  # 幂次结果痕迹
    # 列表横排带跳过条数/已回滚次数/策略标题
    assert b2["skip_count"] == 2 and b2["rollback_count"] == 0
    assert b2["strategy_title"] == "遇撞跳过计数"


def test_all_collisions_fully_skipped(client, auth):
    _post_landing(client, auth, 1, "skip_count", ["x"])
    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["x"])
    b2 = wait_status(client, auth, r2.json["id"], {"全部撞钥跳过"})
    assert b2["success_count"] == 0 and b2["skip_count"] == 1


# ---------- 幂等核对：撞钥整批回滚，撤回到入库前 ----------

def test_rollback_batch_strategy_full_withdraw(client, auth):
    _req, r1 = _post_landing(client, auth, 2, "rollback_batch", ["p1", "p2"])
    wait_status(client, auth, r1.json["id"], {"已入库"})

    _req, r2 = _post_landing(client, auth, 2, "rollback_batch", ["p2", "p9"])
    b2 = wait_status(client, auth, r2.json["id"], {"整批已回滚"})
    assert b2["success_count"] == 0 and b2["rollback_count"] == 1

    detail = client.get(f"/api/batches/{b2['id']}", headers=auth)[1].json
    auto = [o for o in detail["rollback_orders"] if o["requestor"] == "system"]
    assert auto  # 系统自行撤回也开回滚单
    rb = client.get(f"/api/rollbacks/{auto[0]['id']}", headers=auth)[1].json
    assert "回滚单" in rb["body"] and "整批撤回" in rb["body"]  # 回滚单正文可看
    assert any(e["event_type"] == "auto_rolled_back" for e in rb["events"])
    # 撤回到入库前：p9 不能留下活跃记录
    assert all(r["state"] != "active" for r in detail["records"] if r["idem_key"] == "p9")


# ---------- 回滚：缘故必填、本人不得回滚、他人可回滚、后台收尾 ----------

def test_rollback_requires_reason_and_other_operator(client, auth):
    _req, r1 = _post_landing(client, auth, 3, "skip_count", ["q1", "q2"])
    bid = wait_status(client, auth, r1.json["id"], {"已入库"})["id"]

    # 本人不得亲自回滚
    _req, self_rb = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                                json={"reason": "我后悔了", "operator": "admin"})
    assert self_rb.status == 403 and "亲自" in self_rb.json["detail"]

    # 缘故必填
    _req, no_reason = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                                  json={"reason": "  ", "operator": "op_zhou"})
    assert no_reason.status == 400 and "缘故" in no_reason.json["detail"]

    # 他人带缘故发起 → 后台收尾
    _req, ok = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                           json={"reason": "上游数据口径错误，需撤回重灌", "operator": "op_zhou"})
    assert ok.status == 200
    rid = ok.json["id"]
    finished = wait_rollback(client, auth, rid)
    assert "上游数据口径错误" in finished["body"]
    assert finished["batch"]["rollback_count"] == 1
    assert all(r["state"] == "rolled_back" for r in finished["records"])
    # 回滚详情旁可见幂次结果痕迹（accepted 事件随单可核对）
    assert any(e["event_type"] == "manual_rolled_back" for e in finished["events"])


def test_rollback_releases_keys_for_relanding(client, auth):
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["v1"])
    bid = wait_status(client, auth, r1.json["id"], {"已入库"})["id"]
    _req, ok = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                           json={"reason": "测试钥条释放", "operator": "op_wu"})
    wait_rollback(client, auth, ok.json["id"])
    # 回滚后钥条释放，同钥可再次入库成功
    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["v1"])
    b2 = wait_status(client, auth, r2.json["id"], {"已入库"})
    assert b2["success_count"] == 1 and b2["skip_count"] == 0


# ---------- 争用：同流双方争抢，后到败绩；在途禁再入库/回滚 ----------

def test_contention_later_request_loses(client, auth, monkeypatch):
    monkeypatch.setattr("app.domain.Config.LANDER_DELAY", 0.6)
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["c1", "c2"])
    assert r1.json["status"] == "处理中"
    # 后到争抢 → 409 争用败绩
    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["c3"])
    assert r2.status == 409 and "争用" in r2.json["detail"]
    b1 = wait_status(client, auth, r1.json["id"], {"已入库"})
    assert b1["success_count"] == 2
    # 争用败绩在入库列表与事件流可看
    _req, batches = client.get("/api/batches", headers=auth)
    lost = [b for b in batches.json["items"] if b["status"] == "争用败绩"]
    assert lost
    detail = client.get(f"/api/batches/{lost[0]['id']}", headers=auth)[1].json
    assert any(e["event_type"] == "contention" for e in detail["events"])


def test_no_landing_or_rollback_while_inflight(client, auth, monkeypatch):
    monkeypatch.setattr("app.domain.Config.LANDER_DELAY", 0.6)
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["z1"])
    bid = r1.json["id"]
    # 入库未收尾时回滚也被拦
    _req, rb = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                           json={"reason": "想插队回滚", "operator": "op_zhou"})
    assert rb.status == 409
    wait_status(client, auth, bid, {"已入库"})


def test_no_new_landing_while_rollback_open(client, auth, monkeypatch):
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["m1"])
    bid = wait_status(client, auth, r1.json["id"], {"已入库"})["id"]
    monkeypatch.setattr("app.domain.Config.ROLLBACK_DELAY", 0.6)
    _req, ok = client.post(f"/api/batches/{bid}/rollback", headers=auth,
                           json={"reason": "回滚未收尾期间禁入库", "operator": "op_zhou"})
    assert ok.status == 200
    _req, blocked = _post_landing(client, auth, 1, "skip_count", ["m2"])
    assert blocked.status == 409
    wait_rollback(client, auth, ok.json["id"])


# ---------- 入库列表横排字段 + 回滚详情聚合 ----------

def test_batch_list_carries_skip_rollback_policy(client, auth):
    _req, r1 = _post_landing(client, auth, 1, "skip_count", ["a", "b"])
    wait_status(client, auth, r1.json["id"], {"已入库"})
    _req, r2 = _post_landing(client, auth, 1, "skip_count", ["b"])
    wait_status(client, auth, r2.json["id"], {"全部撞钥跳过"})
    _req, listing = client.get("/api/batches", headers=auth)
    for b in listing.json["items"]:
        assert "skip_count" in b and "rollback_count" in b and "strategy_title" in b
    # 回滚详情 404
    _req, miss = client.get("/api/rollbacks/9999", headers=auth)
    assert miss.status == 404


def test_unauthenticated_rejected(client):
    _req, resp = client.get("/api/batches")
    assert resp.status == 401
    _req, resp2 = client.post("/api/flows/1/landing",
                              json={"strategy": "skip_count", "keys": ["x"]})
    assert resp2.status == 401


# ---------- 重启恢复：在途孤儿不永久占用争用闸 ----------

def test_orphan_inflight_recovered_on_boot(client, auth):
    from app.db import get_db
    from app import domain
    # 手工制造一个卡在“处理中”的批次（模拟服务宕机）
    with get_db() as conn:
        now = domain._now()
        bid = conn.execute(
            "INSERT INTO landing_batch (flow_id, strategy, strategy_title, status, "
            "operator, requested_count, note, created_at, updated_at) "
            "VALUES (1,'skip_count','遇撞跳过计数','处理中','admin',1,'x',?,?)",
            (now, now),
        ).lastrowid
        conn.execute(
            "INSERT INTO landing_record (batch_id, flow_id, idem_key, state, created_at) "
            "VALUES (?,?,?,'pending',?)", (bid, 1, "orphan-key", now),
        )
        domain._inflight_flows.add(1)
    # 重启：进程内闸清空 + 启动清扫（清掉一次性守卫以模拟新进程）
    domain._inflight_flows.clear()
    domain._recovered_dbs.clear()
    with get_db() as conn:
        domain.recover_orphans(conn)
    with get_db() as conn:
        assert domain.get_batch(conn, bid)["status"] == "处理失败"
        assert not domain.is_flow_busy(conn, 1)  # 争用闸已释放，可重新入库
    _req, land = _post_landing(client, auth, 1, "skip_count", ["new-key"])
    assert land.status == 200
    b = wait_status(client, auth, land.json["id"], {"已入库"})
    assert b["success_count"] == 1
