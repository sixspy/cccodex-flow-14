from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from sanic import Request, Sanic, json, redirect
from sanic.response import file

from app.config import Config
from app.db import get_db, get_flow, init_db, list_flows, run_extract, run_land
from app import domain

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ser = URLSafeSerializer(Config.SECRET, salt="cc-flow-session")

app = Sanic("cc-flow-lab")
app.static("/static", str(STATIC), name="static")


def _user(request: Request) -> str | None:
    raw = request.cookies.get("flow_session")
    if not raw:
        return None
    try:
        data = ser.loads(raw)
        u = data.get("user") if isinstance(data, dict) else None
        return u if u == Config.ADMIN_USER else None
    except BadSignature:
        return None


def _require_user(request: Request):
    u = _user(request)
    if not u:
        return None, json({"detail": "未登录"}, status=401)
    return u, None


def _actor(request: Request, body: dict) -> str:
    # 操作人可在界面切换（用于演示“本人不得回滚”），缺省取登录账号
    actor = str(body.get("operator") or "").strip()
    return actor or _user(request) or ""


def _int_id(raw: str):
    # 当前依赖组合下 sanic-routing 的 <int:..> 转换器失效，统一裸参数 + 手工校验
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@app.listener("before_server_start")
async def _boot(_app, _loop):
    init_db()
    with get_db() as conn:
        domain.recover_orphans(conn)


@app.get("/")
async def root(request: Request):
    return redirect("/app/" if _user(request) else "/login")


@app.get("/login")
async def login_page(request: Request):
    return await file(STATIC / "login.html")


@app.get("/app/", name="spa_root")
async def spa_root(request: Request):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.get("/app/<path:path>", name="spa_path")
async def spa_path(request: Request, path: str = ""):
    if not _user(request):
        return redirect("/login")
    return await file(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = request.json or {}
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json({"ok": False, "error": "账号或密码错误"}, status=400)
    resp = json({"ok": True, "user": user})
    resp.add_cookie(
        "flow_session",
        ser.dumps({"user": user}),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    resp = json({"ok": True})
    resp.delete_cookie("flow_session", path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user = _user(request)
    if not user:
        return json({"detail": "未登录"}, status=401)
    return json({"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"})


@app.get("/api/flows")
async def api_flows(request: Request):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    with get_db() as conn:
        return json({"items": list_flows(conn)})


@app.get("/api/flows/<flow_id>")
async def api_flow(request: Request, flow_id: str):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    fid = _int_id(flow_id)
    if fid is None:
        return json({"detail": "流不存在"}, status=404)
    with get_db() as conn:
        item = get_flow(conn, fid)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id>/extract")
async def api_extract(request: Request, flow_id: str):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    fid = _int_id(flow_id)
    if fid is None:
        return json({"detail": "流不存在"}, status=404)
    with get_db() as conn:
        item = run_extract(conn, fid)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id>/land")
async def api_land(request: Request, flow_id: str):
    if not _user(request):
        return json({"detail": "未登录"}, status=401)
    fid = _int_id(flow_id)
    if fid is None:
        return json({"detail": "流不存在"}, status=404)
    with get_db() as conn:
        item = run_land(conn, fid)
    if not item:
        return json({"detail": "流不存在"}, status=404)
    return json(item)


# ---------- 幂次策略：当日钉死 ----------

@app.get("/api/policy")
async def api_policy_get(request: Request):
    user, err = _require_user(request)
    if err:
        return err
    with get_db() as conn:
        return json({
            "today": domain.get_today_policy(conn),
            "options": [
                {"strategy": k, "title": v} for k, v in domain.STRATEGIES.items()
            ],
            "event_titles": domain.EVENT_TITLES,
        })


@app.post("/api/policy")
async def api_policy_pin(request: Request):
    user, err = _require_user(request)
    if err:
        return err
    body = request.json or {}
    strategy = str(body.get("strategy") or "").strip()
    try:
        with get_db() as conn:
            row = domain.pin_policy(conn, strategy, _actor(request, body))
    except domain.DomainError as exc:
        return json({"detail": exc.message}, status=exc.status)
    return json(row)


# ---------- 入库批次 ----------

@app.get("/api/batches")
async def api_batches(request: Request):
    user, err = _require_user(request)
    if err:
        return err
    flow_id = request.args.get("flow_id")
    with get_db() as conn:
        fid = int(flow_id) if flow_id else None
        items = domain.list_batches(conn, fid)
        policy = domain.get_today_policy(conn)
    # 入库列表横排：跳过条数、已回滚次数、策略标题随批次带出
    return json({"items": items, "policy": policy})


@app.get("/api/batches/<batch_id>")
async def api_batch_detail(request: Request, batch_id: str):
    user, err = _require_user(request)
    if err:
        return err
    bid = _int_id(batch_id)
    if bid is None:
        return json({"detail": "批次不存在"}, status=404)
    with get_db() as conn:
        item = domain.batch_detail(conn, bid)
    if not item:
        return json({"detail": "批次不存在"}, status=404)
    return json(item)


@app.post("/api/flows/<flow_id>/landing")
async def api_landing(request: Request, flow_id: str):
    user, err = _require_user(request)
    if err:
        return err
    fid = _int_id(flow_id)
    if fid is None:
        return json({"detail": "流不存在"}, status=404)
    body = request.json or {}
    strategy = str(body.get("strategy") or "").strip()
    raw = body.get("keys") or []
    if isinstance(raw, str):
        raw = [k for k in raw.replace("，", ",").replace("\n", ",").split(",")]
    keys = list(raw)
    actor = _actor(request, body)
    with get_db() as conn:
        try:
            batch = domain.create_landing(conn, fid, strategy, actor, keys)
        except domain.DomainError as exc:
            return json({"detail": exc.message}, status=exc.status)
        batch_id = batch["id"]
    # 事务提交后交后台线程做齐去重
    domain.start_landing_worker(batch_id)
    with get_db() as conn:
        return json(domain.get_batch(conn, batch_id))


# ---------- 回滚单 ----------

@app.post("/api/batches/<batch_id>/rollback")
async def api_rollback_create(request: Request, batch_id: str):
    user, err = _require_user(request)
    if err:
        return err
    bid = _int_id(batch_id)
    if bid is None:
        return json({"detail": "批次不存在"}, status=404)
    body = request.json or {}
    reason = str(body.get("reason") or "")
    requestor = _actor(request, body)
    with get_db() as conn:
        try:
            order = domain.create_rollback(conn, bid, requestor, reason)
        except domain.DomainError as exc:
            return json({"detail": exc.message}, status=exc.status)
        rollback_id = order["id"]
    domain.start_rollback_worker(rollback_id)
    with get_db() as conn:
        return json(domain.rollback_detail(conn, rollback_id))


@app.get("/api/rollbacks/<rollback_id>")
async def api_rollback_detail(request: Request, rollback_id: str):
    user, err = _require_user(request)
    if err:
        return err
    rid = _int_id(rollback_id)
    if rid is None:
        return json({"detail": "回滚单不存在"}, status=404)
    with get_db() as conn:
        item = domain.rollback_detail(conn, rid)
    if not item:
        return json({"detail": "回滚单不存在"}, status=404)
    return json(item)
