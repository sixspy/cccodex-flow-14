/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '', lab: '', items: [], selected: null,
  actor: '', policy: null, options: [],
  batches: [], detailBatch: null, detailRollback: null,
  strategy: '', keysText: '', reason: '', notice: '', busy: false,
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || '请求失败');
  return j;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.actor = me.user;
    state.lab = me.lab;
    await refreshAll();
    if (!state.strategy && state.options.length) state.strategy = state.options[0].strategy;
    render();
    const hasInflight = state.batches.some((x) => ['处理中', '回滚中'].includes(x.status));
    if (!state.policy || hasInflight) tick();
  } catch (e) {
    location.href = '/login';
  }
}

async function refreshAll() {
  state.items = (await api('/api/flows')).items;
  const p = await api('/api/policy');
  state.policy = p.today;
  state.options = p.options;
  if (state.policy) state.strategy = state.policy.strategy;
  else if (!state.strategy && state.options.length) state.strategy = state.options[0].strategy;
  const b = await api('/api/batches');
  state.batches = b.items;
  if (state.selected) state.selected = await api('/api/flows/' + state.selected.id);
}

/* 后台处理中轮询：入库去重与回滚在后台做齐，界面跟进状态收尾 */
function tick() {
  setTimeout(async () => {
    try {
      await refreshAll();
      if (state.detailBatch) state.detailBatch = await api('/api/batches/' + state.detailBatch.id);
      if (state.detailRollback) state.detailRollback = await api('/api/rollbacks/' + state.detailRollback.id);
      render();
      const pending = state.batches.some((x) => ['处理中', '回滚中'].includes(x.status));
      if (pending) tick();
    } catch (e) { /* 保留当前界面 */ }
  }, 600);
}

async function select(id) {
  state.selected = await api('/api/flows/' + id);
  state.detailBatch = null;
  state.detailRollback = null;
  state.notice = '';
  render();
}

async function doExtract() {
  state.selected = await api('/api/flows/' + state.selected.id + '/extract', { method: 'POST' });
  await refreshAll();
  render();
}

async function pinPolicy(strategy) {
  try {
    state.policy = await api('/api/policy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy, operator: state.actor }),
    });
    state.strategy = state.policy.strategy;
    state.notice = '幂次策略今日已钉死：' + state.policy.title;
  } catch (e) { state.notice = e.message; }
  render();
}

async function doLanding() {
  if (!state.selected) return;
  const keys = state.keysText.split(/[\n,，]/).map((k) => k.trim()).filter(Boolean);
  try {
    const batch = await api('/api/flows/' + state.selected.id + '/landing', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy: state.strategy, keys, operator: state.actor }),
    });
    state.notice = '批次 #' + batch.id + ' 已开入库，去重在后台做齐';
    state.keysText = '';
    await refreshAll();
    tick();
  } catch (e) {
    state.notice = '入库被拦：' + e.message;
    await refreshAll();
  }
  render();
}

async function openBatch(id) {
  state.detailBatch = await api('/api/batches/' + id);
  state.detailRollback = null;
  render();
}

async function openRollback(rid) {
  state.detailRollback = await api('/api/rollbacks/' + rid);
  render();
}

async function doRollback(batchId) {
  if (!state.reason.trim()) { state.notice = '回滚必须写明缘故'; render(); return; }
  try {
    const order = await api('/api/batches/' + batchId + '/rollback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: state.reason, operator: state.actor }),
    });
    state.notice = '回滚单 #' + order.id + ' 已开，回滚在后台收尾';
    state.reason = '';
    await refreshAll();
    tick();
  } catch (e) {
    state.notice = '回滚被拦：' + e.message;
    await refreshAll();
  }
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

const EVENT_LABEL = {
  accepted: '入库成功', skipped: '幂等撞钥跳过', auto_rolled_back: '撞钥整批回滚',
  manual_rolled_back: '人工回滚', contention: '争用入库败绩',
  empty_key: '空钥条败绩', policy_drift: '策略漂移拦截', recovered: '重启中断恢复',
};

function policyBar() {
  const opts = state.options.map((o) => {
    const pinned = state.policy && state.policy.strategy === o.strategy;
    const locked = !!state.policy;
    return `<button type="button" class="${pinned ? 'pinned' : 'ghost'}"
      data-pin="${o.strategy}" ${locked && !pinned ? 'disabled title="当日策略已钉死，不许更换"' : ''}>
      ${pinned ? '📌 ' : ''}${o.title}</button>`;
  }).join('');
  const info = state.policy
    ? `<span class="chip ok">今日钉死：${esc(state.policy.title)}</span>
       <span class="mono">钉于人 ${esc(state.policy.pinned_by)} · ${esc(state.policy.pinned_at)}</span>`
    : '<span class="chip warn">今日尚未钉死策略，首次入库自动钉死，钉后当天不许更换</span>';
  return `<section class="panel-line"><strong>幂次策略（当日钉死一种）</strong>
    <div class="hstack">${opts}</div>${info}</section>`;
}

function landingForm() {
  const flowBatches = state.batches.filter((b) => state.selected && b.flow_id === state.selected.id);
  const busy = flowBatches.some((b) => ['处理中', '回滚中'].includes(b.status));
  return `
    <section class="detail">
      <h2>${esc(state.selected.title)}</h2>
      <p class="mono">${esc(state.selected.source_uri)}</p>
      <p>状态 <span class="chip">${esc(state.selected.status)}</span> · 已抽取 ${state.selected.extracted} · 已落库 ${state.selected.landed}</p>
      <div class="actions"><button type="button" id="btn-ex">执行抽取</button></div>
      <h3>幂等入库</h3>
      <label class="hstack">处置策略
        <select id="strategy">
          ${state.options.map((o) => `<option value="${o.strategy}" ${o.strategy === state.strategy ? 'selected' : ''}>${o.title}</option>`).join('')}
        </select>
        ${state.policy ? '<span class="chip ok">与今日钉死策略对齐</span>' : '<span class="chip warn">未钉死，提交即钉</span>'}
      </label>
      <label>幂等钥条（每行一条或逗号分隔，空缺禁止入库）
        <textarea id="keys" rows="3" placeholder="key-1&#10;key-2"></textarea>
      </label>
      <div class="actions">
        <button type="button" id="btn-land" ${busy ? 'disabled title="回滚/入库未收尾，禁止再次开入库"' : ''}>
          ${busy ? '在途处理中…' : '点入库（后台做齐去重）'}
        </button>
      </div>
    </section>`;
}

function batchTable() {
  const rows = state.batches.map((b) => `
    <tr class="${state.detailBatch && state.detailBatch.id === b.id ? 'on' : ''}" data-batch="${b.id}">
      <td>#${b.id}</td>
      <td>${b.flow_id}</td>
      <td>${esc(b.strategy_title)}</td>
      <td><span class="chip st-${b.status === '已入库' || b.status === '部分跳过' ? 'ok' : ''}">${esc(b.status)}</span></td>
      <td>${b.requested_count}</td>
      <td>${b.success_count}</td>
      <td class="num-skip">${b.skip_count}</td>
      <td class="num-rb">${b.rollback_count}</td>
      <td>${esc(b.operator)}</td>
      <td class="mono">${esc(b.note)}</td>
    </tr>`).join('');
  return `<section class="panel-line">
    <h3>入库列表（横排：跳过条数 · 已回滚次数 · 策略标题）</h3>
    <table class="grid">
      <thead><tr><th>批次</th><th>流</th><th>策略标题</th><th>状态</th>
        <th>提交</th><th>成功</th><th>跳过条数</th><th>已回滚次数</th><th>入库人</th><th>备注</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="10" class="hint">暂无批次</td></tr>'}</tbody>
    </table>
  </section>`;
}

function eventList(events) {
  if (!events || !events.length) return '<p class="hint">暂无幂次结果痕迹</p>';
  return `<ul class="events">${events.map((e) => `
    <li><span class="chip ev-${esc(e.event_type)}">${esc(EVENT_LABEL[e.event_type] || e.event_type)}</span>
      ${esc(e.message)} ${e.idem_key ? `<span class="mono">[${esc(e.idem_key)}]</span>` : ''}
      <span class="mono dim">${esc(e.created_at)} ${esc(e.operator)}</span></li>`).join('')}</ul>`;
}

function batchDetailPanel() {
  const b = state.detailBatch;
  if (!b) return '';
  const canRollback = !['处理中', '回滚中', '整批已回滚', '已回滚', '空钥败绩', '争用败绩', '策略漂移拦截', '全部撞钥跳过'].includes(b.status);
  const rbOrders = (b.rollback_orders || []).map((o) => `
    <li>回滚单 #${o.id} · <span class="chip">${esc(o.status)}</span> · 发起人 ${esc(o.requestor)}
      <button type="button" class="link" data-rb="${o.id}">查看回滚详情/正文</button></li>`).join('');
  return `<section class="detail detail-box">
    <h3>批次 #${b.id} 详情 · 幂次结果痕迹</h3>
    <p>${esc(b.strategy_title)} · 提交 ${b.requested_count} · 成功 ${b.success_count}
       · <b>跳过 ${b.skip_count}</b> · <b>已回滚 ${b.rollback_count} 次</b> · 入库人 ${esc(b.operator)}</p>
    ${eventList(b.events)}
    <h4>钥条</h4>
    <ul class="keys">${(b.records || []).map((r) => `<li><span class="mono">${esc(r.idem_key)}</span> <span class="chip">${esc(r.state)}</span></li>`).join('') || '<li class="hint">无</li>'}</ul>
    <h4>回滚单</h4>
    <ul class="keys">${rbOrders || '<li class="hint">暂无回滚单</li>'}</ul>
    ${canRollback ? `
      <label>回滚缘故（必填；不可由入库人 ${esc(b.operator)} 本人发起）
        <textarea id="reason" rows="2" placeholder="写明回滚缘故…"></textarea>
      </label>
      <div class="actions"><button type="button" id="btn-rb" data-rbbatch="${b.id}">发起回滚（后台收尾）</button></div>`
      : '<p class="hint">当前状态不可回滚（在途/已回滚/败绩批次）</p>'}
  </section>`;
}

function rollbackPanel() {
  const o = state.detailRollback;
  if (!o) return '';
  return `<section class="detail detail-box rb">
    <h3>回滚详情 · 回滚单 #${o.id} 正文</h3>
    <pre class="order-body">${esc(o.body)}</pre>
    <p>状态 <span class="chip ${o.status === '已完成' ? 'ok' : 'warn'}">${esc(o.status)}</span>
       · 发起人 ${esc(o.requestor)} · 入库人 ${esc(o.batch ? o.batch.operator : '')}
       · 收尾 ${esc(o.finished_at || '未收尾')}</p>
    <h4>争用/幂次痕迹（同屏可核对）</h4>
    ${eventList(o.events)}
  </section>`;
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code)}</span>
      <span class="chip">${esc(it.status)}</span>
    </button>`).join('');
  const detail = state.selected ? landingForm() : '<p class="hint">从左侧选择一条编排流</p>';
  el.innerHTML = `
    <header class="bar">
      <div class="brand">${esc(state.lab)}</div>
      <div class="hstack">
        <label class="actor">操作人
          <select id="actor">
            ${['admin', 'op_zhou', 'op_wu'].map((a) => `<option value="${a}" ${a === state.actor ? 'selected' : ''}>${a}</option>`).join('')}
          </select>
        </label>
        <span>${esc(state.user)} · <button type="button" id="out" class="link">退出</button></span>
      </div>
    </header>
    ${policyBar()}
    ${state.notice ? `<div class="notice" id="notice">${esc(state.notice)}</div>` : ''}
    <div class="layout">
      <aside>${rows}</aside>
      <main>
        ${detail}
        ${batchTable()}
        <div class="detail-grid">${batchDetailPanel()}${rollbackPanel()}</div>
      </main>
    </div>`;
  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  el.querySelectorAll('tr[data-batch]').forEach((tr) => { tr.onclick = () => openBatch(+tr.dataset.batch); });
  el.querySelectorAll('[data-rb]').forEach((btn) => { btn.onclick = (e) => { e.stopPropagation(); openRollback(+btn.dataset.rb); }; });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLanding;
  const brb = el.querySelector('#btn-rb'); if (brb) brb.onclick = () => doRollback(+brb.dataset.rbbatch);
  el.querySelectorAll('[data-pin]').forEach((btn) => { btn.onclick = () => pinPolicy(btn.dataset.pin); });
  const sel = el.querySelector('#strategy'); if (sel) sel.onchange = (e) => { state.strategy = e.target.value; };
  const kt = el.querySelector('#keys'); if (kt) { kt.value = state.keysText; kt.oninput = (e) => { state.keysText = e.target.value; }; }
  const rs = el.querySelector('#reason'); if (rs) { rs.value = state.reason; rs.oninput = (e) => { state.reason = e.target.value; }; }
  const ac = el.querySelector('#actor'); if (ac) ac.onchange = (e) => { state.actor = e.target.value; };
}

boot();
