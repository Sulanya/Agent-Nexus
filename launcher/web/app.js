/* Agent Nexus launcher UI — v1.8.0
   Backend contract lives in launcher/app.py. This file is loaded by pywebview
   and also works as a plain browser demo (?demo=ready / ?demo=expired). */
(function(){
  'use strict';
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const qs = new URLSearchParams(location.search);
  const DEMO = qs.get('demo') || '';
  const SKIPBOOT = qs.get('skipboot') === '1';
  const FALLBACK_VERSION = '1.8.0';
  const APP_VERSION = String(qs.get('ver') || FALLBACK_VERSION).replace(/^v/i, '');
  const API_TIMEOUT = 20000;
  // 首次启动要收旧进程、起网关、等 Cloudflare 回话，实测 30–90 秒；
  // 旧版统一 20 秒就 abort，于是后端还在正常启动，界面先报"失败"。
  const API_TIMEOUTS = {start_stack: 170000, boot: 70000, run_checks: 70000, trial_status: 30000};

  function apiErrorText(data, status){
    const e = data && data.error;
    if (e && typeof e === 'object') return String(e.message || e.code || ('HTTP ' + status));
    if (e) return String(e);
    return 'HTTP ' + status;
  }

  async function httpCall(name, args){
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), API_TIMEOUTS[name] || API_TIMEOUT);
    try {
      const r = await fetch('/api/' + name, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({args: args || []}),
        cache: 'no-store',
        signal: ctl.signal
      });
      const text = await r.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) {}
      // 业务失败用 {ok:false,error:...} 返回，交给调用方展示；
      // 只有 HTTP 层失败（例如自护层 418）才在这里抛错。
      if (!r.ok) {
        const err = new Error(apiErrorText(data, r.status));
        if (data && data.error && typeof data.error === 'object') err.code = data.error.code || '';
        throw err;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  /* Same-origin HTTP bridge.  pywebview's injected js_api object is not
     reliable on a cold second launch; this contract works identically in
     WebView2, Chrome and the mobile companion page. */
  const api = () => DEMO ? null : ({
    boot: () => httpCall('boot'),
    run_checks: () => httpCall('run_checks'),
    start_stack: () => httpCall('start_stack'),
    stop_stack: () => httpCall('stop_stack'),
    trial_status: () => httpCall('trial_status'),
    renew: code => httpCall('renew', [code]),
    copy_text: text => httpCall('copy_text', [text]),
    open_external: link => httpCall('open_external', [link]),
    open_folder: which => httpCall('open_folder', [which]),
    minimize_app: () => httpCall('minimize_app'),
    quit_app: () => httpCall('quit_app'),
    js_log: message => httpCall('js_log', [message]),
    ui_ready: () => httpCall('ui_ready')
  });
  const apiLog = message => {
    if (DEMO) return;
    httpCall('js_log', [String(message || '')]).catch(() => {});
  };
  // Heartbeat for the desktop shell: if WebView2 stays blank, app.py opens the
  // same console in the default browser instead of leaving a white window.
  if (!DEMO) api().ui_ready().catch(() => {});
  if (qs.get('instant') === '1') document.documentElement.classList.add('noanim');
  const S = {
    version: APP_VERSION, trialMode: 'active', trialUntil: 0, trialTotal: 3600,
    trialId: '', running: false, link: '', qr: '', starting: false,
    blocked: false, engineOk: false, backendOk: false, booting: false, port: 8790
  };
  const verChip0 = $('#verChip'); if (verChip0) verChip0.textContent = 'v' + APP_VERSION;
  window.addEventListener('error', ev => {
    apiLog('js error: ' + (ev.message || '') + ' @ ' + (ev.filename || '') + ':' + (ev.lineno || 0));
  });
  window.addEventListener('unhandledrejection', ev => {
    apiLog('js rejection: ' + String((ev.reason && ev.reason.message) || ev.reason || ''));
  });
  const IDS = ['env','codex','claude','workbuddy','proxy','network','parity','gateway','tunnel'];
  const BADGE = {ok:'通过',run:'检测中',warn:'注意',fail:'未通过',idle:'待检测'};
  let bootPayload = null;
  let exDestructUntil = 0;
  let exTimers = [];

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  /* ---------- boot overlay ---------- */
  const wm = $('#bootWm');
  if (wm) 'AGENT NEXUS'.split('').forEach((c, i) => {
    const s = document.createElement('span');
    s.textContent = c === ' ' ? '\u00a0' : c;
    s.style.animationDelay = (1.0 + i * 0.035) + 's';
    wm.appendChild(s);
  });
  const bootMsgs = [
    ['正在加载本机控制台', 0],
    ['正在读取 Codex / Claude Code / WorkBuddy 配置', 700],
    ['正在准备本机网关与配对码', 1400]
  ];
  bootMsgs.forEach(([text, delay]) => setTimeout(() => {
    const m = $('#bootMsg'); if (m) m.textContent = text;
  }, delay));

  function bootDone(){
    const b = $('#boot');
    if (!b || b.dataset.done) return;
    b.dataset.done = '1';
    b.classList.add('gone');
    setTimeout(() => b.remove(), 720);
    const app = $('#app'); if (app) app.classList.add('in');
  }

  // 后端偶尔会因为 WebView / 网关启动慢而迟迟不回话。开机层不能一直盖着，
  // 否则用户看到的就是一块"卡死的黑屏"。到点先放行到控制台，后台继续连。
  setTimeout(() => {
    const b = $('#boot');
    if (!b || b.dataset.done) return;
    const m = $('#bootMsg'); if (m) m.textContent = '后端还在启动，先进入控制台…';
    bootDone();
  }, 9000);

  function waitApi(ms){
    return new Promise(resolve => {
      let done = false;
      const started = Date.now();
      const finish = v => { if (done) return; done = true; resolve(v); };
      const tick = async () => {
        if (done) return;
        try {
          const r = await fetch('/api/health', {cache:'no-store'});
          if (r.ok) {
            const data = await r.json();
            if (data && data.ok) return finish(true);
          }
        } catch (e) {}
        if (Date.now() - started >= (ms || 30000)) return finish(false);
        setTimeout(tick, 150);
      };
      tick();
    });
  }

  /* ---------- self check ---------- */
  function setCheck(id, state, sub, badge){
    const el = document.querySelector('.chk[data-id="' + id + '"]');
    if (!el) return;
    el.dataset.state = state;
    if (sub) el.querySelector('.ct i').textContent = sub;
    el.querySelector('.cs').textContent = badge || BADGE[state] || '';
  }
  function resetChecks(){
    IDS.forEach(id => {
      const el = document.querySelector('.chk[data-id="' + id + '"]');
      if (!el) return;
      el.dataset.state = 'idle';
      el.querySelector('.cs').textContent = '待检测';
      el.querySelector('.ct i').textContent = '正在读取…';
    });
  }

  function applyNetworkStatus(network){
    if (!network) return;
    if (!document.querySelector('.chk[data-id="network"]')) {
      const anchor = document.querySelector('.chk[data-id="parity"]');
      if (anchor) {
        const row = document.createElement('div');
        row.className = 'chk';
        row.dataset.id = 'network';
        row.dataset.state = 'idle';
        row.innerHTML = '<span class="ring"><svg viewBox="0 0 24 24"><circle class="tr" cx="12" cy="12" r="10"/><circle class="arc" cx="12" cy="12" r="10"/></svg><span class="ick">⌁</span></span><span class="ct"><b>网络</b><i>正在读取…</i></span><span class="cs">待检测</span>';
        anchor.parentNode.insertBefore(row, anchor);
      }
    }
    const bits = [];
    if (network.lanIp) bits.push(network.lanIp + (network.prefix ? '/' + network.prefix : ''));
    if (network.gateway) bits.push('网关 ' + network.gateway);
    if (network.interface) bits.push(network.interface);
    const state = network.lanIp && network.gateway ? 'ok' : 'warn';
    setCheck('network', state, bits.join(' · ') || '未取得默认网关；不修改系统网络',
      network.enterprise ? '局域网' : '已读取');
    const tunnel = document.querySelector('.chk[data-id="tunnel"] .ct b');
    const tunnelSub = document.querySelector('.chk[data-id="tunnel"] .ct i');
    if (network.enterprise) {
      if (tunnel) tunnel.textContent = '局域网通道';
      if (tunnelSub) tunnelSub.textContent = '手机需接入同一企业网络';
    }
    const gatewaySub = document.querySelector('.chk[data-id="gateway"] .ct i');
    if (gatewaySub && network.bindHost) gatewaySub.textContent = network.bindHost + ':' + (network.port || 8790);
  }
  const DEMO_ROWS = [
    {id:'env', state:'ok', sub:'Windows 11 · 内置运行时 3.11 · 目录可写', badge:'通过'},
    {id:'codex', state:'ok', sub:'codex-cli 0.48 · deepseek-flash · deepseek · 思考 max', badge:'已就绪'},
    {id:'claude', state:'ok', sub:'claude 2.1 · deepseek-flash · 思考 xhigh · api.deepseek.com', badge:'已就绪'},
    {id:'workbuddy', state:'ok', sub:'codebuddy · absen-auto · AOne-Api · 192.168.196.71', badge:'已就绪'},
    {id:'proxy', state:'warn', sub:'系统代理已开启 · 如遇断流请检查代理绕过', badge:'注意'},
    {id:'network', state:'ok', sub:'局域网地址 · 默认网关已读取', badge:'局域网'},
    {id:'parity', state:'ok', sub:'开发源码目录 · 未启用发行封签', badge:'开发模式'},
    {id:'gateway', state:'warn', sub:'127.0.0.1:8790 · 未启动', badge:'未启动'},
    {id:'tunnel', state:'ok', sub:'cloudflared · 已就绪', badge:'就绪'}
  ];
  async function runChecks(){
    resetChecks();
    const rb = $('#recheck'); if (rb) rb.classList.add('busy');
    IDS.forEach((id, i) => setTimeout(() => {
      const el = document.querySelector('.chk[data-id="' + id + '"]');
      if (el && el.dataset.state === 'idle') el.dataset.state = 'run';
    }, i * 70));
    let rows = null;
    if (api()) {
      try { rows = await api().run_checks(); } catch (e) { rows = null; }
    }
    let backendDown = false;
    if (!rows || !rows.length) {
      backendDown = true;
      if (api()) rows = IDS.map(id => ({id, state:'warn', sub:'后端响应超时', badge:'重试'}));
      else if (DEMO) rows = DEMO_ROWS;
      else rows = IDS.map(id => ({id, state:'warn', sub:'启动器后端未连接', badge:'连接中'}));
      await sleep(DEMO ? 900 : 180);
    }
    for (const r of rows) {
      setCheck(r.id, r.state, r.sub, r.badge);
      await sleep(130);
    }
    if (rb) rb.classList.remove('busy');
    S.engineOk = rows.some(r => (r.id === 'codex' || r.id === 'claude' || r.id === 'workbuddy') && r.state === 'ok');
    if (backendDown) {
      S.backendOk = false;
      if (!S.blocked) {
        const st = $('#stageTitle'); if (st) st.textContent = '后端响应超时';
        const ss = $('#stageSub'); if (ss) ss.textContent = '界面还在；点「重新检测」再试一次，不用关窗口';
      }
    } else if (!S.engineOk && !S.blocked) {
      $('#stageTitle').textContent = '没找到 AI 引擎';
      $('#stageSub').textContent = '先装一个 Codex / Claude Code / WorkBuddy，登录后再重新检测';
    }
    return rows;
  }

  /* ---------- trial ---------- */
  function fmt(sec){
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  }
  function fmtShort(sec){
    sec = Math.max(0, Math.floor(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    return String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
  }
  const C = 2 * Math.PI * 52;
  function isBlocked(mode){ return false; }
  function paintTrial(){
    const dot = $('#trialDot');
    if (dot) { dot.classList.remove('ended'); dot.classList.add('ok'); }
    const title = $('#trialSub');
    if (title) title.textContent = '自动读取这台电脑的客户端、配置与模型，不限时使用';
  }

  function applyTrial(t){
    S.blocked = false;
    S.trialMode = 'unlimited';
    paintTrial();
  }

  /* ---------- backend boot ---------- */
  async function bootData(){
    if (bootPayload) return bootPayload;
    if (!api()) return null;
    let d = null;
    try { d = await api().boot(); } catch (e) { d = null; }
    if (!d) { S.backendOk = false; return null; }
    bootPayload = d;
    S.backendOk = true;
    S.version = d.version || S.version;
    S.port = Number(d.port) || S.port || 8790;
    applyTrial(d.trial);
    S.running = !!d.running;
    S.link = d.link || '';
    S.qr = d.qr || '';
    applyNetworkStatus(d.network);
    const vc = $('#verChip'); if (vc) vc.textContent = 'v' + S.version;
    const fr = $('#footRight'); if (fr) fr.innerHTML = (d.osName || 'Windows') + ' · <b>v' + S.version + '</b>';
    return d;
  }

  /* ---------- stage / QR ---------- */
  function step(name, cls){
    const el = document.querySelector('.step[data-s="' + name + '"]');
    if (el) el.className = 'step ' + cls;
  }
  function resetSteps(){ $$('.steps .step').forEach(e => e.className = 'step'); }
  function allStepsOk(){ ['gateway','tunnel','qr'].forEach(n => step(n, 'ok')); }

  function setDevWaiting(){
    const dl = $('#devList');
    if (!dl) return;
    dl.innerHTML = '<div class="dev"><span class="di"><svg viewBox="0 0 24 24"><rect x="7" y="2.8" width="10" height="18.4" rx="2.4"/><path d="M10.6 18.6h2.8"/></svg></span><div class="dn"><b>手机 / 平板</b><span>配对码已生成</span></div><span class="st" style="color:#6ee7b7">待连接</span></div>';
  }

  function showQr(link, qr){
    if (!link) return;
    S.link = link; S.running = true;
    S.qr = qr || '';
    const lt = $('#linkTxt'); if (lt) lt.textContent = link.replace(/^https?:\/\//, '');
    const im = $('#qrImg');
    if (im) {
      if (qr) { im.src = qr; im.style.visibility = 'visible'; }
      else { im.removeAttribute('src'); im.style.visibility = 'hidden'; }
    }
    let fb = document.getElementById('qrFallback');
    if (!qr) {
      if (!fb) {
        fb = document.createElement('div');
        fb.id = 'qrFallback';
        fb.textContent = '二维码生成失败\n请复制下方链接';
        fb.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;white-space:pre-line;padding:18px;font:600 13px/1.6 system-ui;color:#333;background:#fff;border-radius:12px';
        const frame = document.querySelector('.qrFrame');
        if (frame) frame.appendChild(fb);
      }
      if (fb) fb.style.display = 'flex';
    } else if (fb) {
      fb.style.display = 'none';
    }
    const qc = $('#qrCard'); if (qc) qc.classList.add('on');
    const ow = $('#orbWrap'); if (ow) ow.classList.add('out');
    const st = $('#stageTitle'); if (st) st.textContent = '已连接';
    const ss = $('#stageSub'); if (ss) ss.textContent = '手机扫码即可接入这台电脑';
    setDevWaiting();
  }
  function hideQr(){
    S.running = false; S.link = ''; S.qr = '';
    const qc = $('#qrCard'); if (qc) qc.classList.remove('on');
    setTimeout(() => { const ow = $('#orbWrap'); if (ow) ow.classList.remove('out'); }, 460);
    const st = $('#stageTitle'); if (st) st.textContent = '已停止';
    const ss = $('#stageSub'); if (ss) ss.textContent = '服务已关闭，电脑不受影响';
    const bt = $('#startTxt'); if (bt) bt.textContent = '重新启动';
    resetSteps();
    setCheck('gateway','warn','127.0.0.1:' + (S.port || 8790) + ' · 已停止','已停止');
    setCheck('tunnel','warn','cloudflared · 已停止','已停止');
  }

  async function waitForStack(maxMs){
    const started = Date.now();
    while (Date.now() - started < maxMs) {
      await sleep(2500);
      bootPayload = null;
      let d = null;
      try { d = await bootData(); } catch (e) { d = null; }
      if (d && d.running && d.link) {
        return {ok:true, link:d.link, qr:d.qr, port:d.port};
      }
    }
    return {ok:false, error:'启动仍在后台进行，但还没拿到二维码。请再等一会儿，点「重新检测」或查看运行日志。'};
  }

  async function start(){
    if (S.starting) return;
    // 已经在运行就只把现有二维码亮出来，绝对不重启隧道：
    // 旧版这里会无条件 start_stack -> 停掉旧隧道，手机上的二维码瞬间失效。
    if (S.running && S.link) { showQr(S.link, S.qr); return; }
    if (!S.backendOk && !DEMO) {
      const st = $('#stageTitle'); if (st) st.textContent = '正在重新连接后端…';
      const ss = $('#stageSub'); if (ss) ss.textContent = '稍等，连接好后会自动继续';
      bootPayload = null;
      await bootSequence(true);
      return;
    }
    if (!S.engineOk && !DEMO) {
      const st = $('#stageTitle'); if (st) st.textContent = '先装一个 AI 引擎';
      const ss = $('#stageSub'); if (ss) ss.textContent = 'Codex / Claude Code / WorkBuddy 至少装一个并登录';
      return;
    }
    S.starting = true;
    const btn = $('#startBtn'); if (btn) btn.classList.add('loading');
    const bt = $('#startTxt'); if (bt) bt.textContent = '正在启动…';
    const st = $('#stageTitle'); if (st) st.textContent = '正在点火';
    const ss = $('#stageSub'); if (ss) ss.textContent = '准备本机网关';
    resetSteps(); step('gateway','on');
    const hintTimers = [
      setTimeout(() => { step('gateway','ok'); step('tunnel','on'); if (ss) ss.textContent = '正在建立外网隧道'; }, 4200),
      setTimeout(() => { step('tunnel','ok'); step('qr','on'); if (ss) ss.textContent = '正在生成本次配对码'; }, 10500),
      setTimeout(() => { if (ss) ss.textContent = '隧道有点慢，还在等 Cloudflare 回话…'; }, 22000)
    ];
    let r = null;
    if (api()) {
      try {
        r = await api().start_stack();
      } catch (e) {
        const msg = (e && e.message) ? e.message : String(e || '');
        const busy = !!(e && (e.name === 'AbortError' || e.code === 'busy'));
        r = {ok:false, busy, error: msg || '启动请求没有返回'};
      }
      if (r && r.busy) {
        if (st) st.textContent = '启动还在进行';
        if (ss) ss.textContent = '首次启动要等 Cloudflare 回话，正在等后台完成…';
        if (bt) bt.textContent = '启动中…';
        setCheck('gateway','warn','启动正在进行中','等待中');
        setCheck('tunnel','warn','等待外网隧道','等待中');
        r = await waitForStack(150000);
      }
    } else if (DEMO) {
      await sleep(1600); step('gateway','ok'); step('tunnel','on');
      await sleep(1100); step('tunnel','ok'); step('qr','on');
      await sleep(450); step('qr','ok');
      r = {ok:true, link:'https://an.example.invalid/?k=demo', qr:'demo-qr.svg'};
    } else {
      r = {ok:false, error:'启动器后端未连接，请关掉窗口重新打开 AgentNexus.exe'};
    }
    hintTimers.forEach(clearTimeout);
    S.starting = false;
    if (btn) btn.classList.remove('loading');
    if (r && r.ok) {
      S.port = Number(r.port) || S.port || 8790;
      allStepsOk();
      showQr(r.link, r.qr);
      setCheck('gateway','ok','127.0.0.1:' + S.port + ' · 已监听','运行中');
      if (r.local) setCheck('tunnel','ok','本机模式 · 仅这台电脑可打开','本机模式');
      else setCheck('tunnel','ok','trycloudflare · 已连通','已连通');
    } else if (r && r.trial && isBlocked(r.trial.mode)) {
      applyTrial(r.trial);
      applyTrial(r.trial);
    } else {
      if (st) st.textContent = '启动失败';
      if (ss) ss.textContent = (r && r.error) || '看 _run\\launcher-start.log';
      if (bt) bt.textContent = '重试启动';
      setCheck('gateway','fail','启动失败','未通过');
      setCheck('tunnel','fail','未建立','未通过');
    }
  }

  async function stop(){
    if (api()) { try { await api().stop_stack(); } catch (e) {} }
    hideQr();
  }

  /* ---------- clipboard / external ---------- */
  async function copyText(text){
    if (api()) { try { await api().copy_text(text); return true; } catch (e) {} }
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { return false; }
  }
  const copyBtn = $('#copyBtn');
  if (copyBtn) copyBtn.addEventListener('click', async () => {
    const ok = await copyText(S.link || '');
    copyBtn.style.color = ok ? '#6ee7b7' : '#fda4b4';
    setTimeout(() => copyBtn.style.color = '', 1000);
  });
  const openBtn = $('#openBtn');
  if (openBtn) openBtn.addEventListener('click', () => {
    if (api()) api().open_external(S.link); else window.open(S.link, '_blank');
  });
  const stopBtn = $('#stopBtn');
  if (stopBtn) stopBtn.addEventListener('click', stop);

  /* ---------- window buttons ---------- */
  const bMin = $('#btnMin'); if (bMin) bMin.addEventListener('click', () => { if (api()) api().minimize_app(); });
  const bClose = $('#btnClose'); if (bClose) bClose.addEventListener('click', () => { if (api()) api().quit_app(); });
  const bFolder = $('#btnFolder'); if (bFolder) bFolder.addEventListener('click', () => { if (api()) api().open_folder('root'); });
  const bLog = $('#btnLog'); if (bLog) bLog.addEventListener('click', () => { if (api()) api().open_folder('run'); });
  const recheck = $('#recheck'); if (recheck) recheck.addEventListener('click', async () => {
    if (!DEMO && !S.backendOk) {
      bootPayload = null;
      await bootSequence(true);
    } else {
      await runChecks();
    }
  });
  const startBtn = $('#startBtn'); if (startBtn) startBtn.addEventListener('click', start);

  /* ---------- public hooks for Python ---------- */
  window.AN = window.AN || {};
  window.AN.check = setCheck;
  window.AN.stage = function(name, state, msg){ step(name, state); if (msg) { const s = $('#stageSub'); if (s) s.textContent = msg; } };
  window.AN.ready = showQr;
  window.AN.trial = applyTrial;
  window.AN.expired = function(){ paintTrial(); };
  window.AN.runChecks = runChecks;

  /* ---------- demo / boot sequence ---------- */
  function demoBoot(){
    S.version = APP_VERSION;
    S.trialMode = 'unlimited'; S.trialUntil = 0;
    S.trialTotal = 0; S.trialId = '';
    const vc = $('#verChip'); if (vc) vc.textContent = 'v' + APP_VERSION;
    const fr = $('#footRight'); if (fr) fr.innerHTML = 'Windows · <b>v' + APP_VERSION + '</b>';
    const tid = $('#trialId'); if (tid) tid.textContent = 'TRIAL-' + S.trialId;
    paintTrial();
    return {
      version:APP_VERSION, osName:'Windows',
      trial:{mode:'unlimited', selfDestructEnabled:false, dataDeletionEnabled:false},
      running: DEMO === 'ready',
      link: DEMO === 'ready' ? 'https://an.example.invalid/?k=demo' : '',
      qr: DEMO === 'ready' ? 'demo-qr.svg' : ''
    };
  }

  function offlineBoot(){
    const d = {
      version:APP_VERSION, osName:'Windows',
      trial:{mode:'off', until:0, total:0, totalHours:1, id:''},
      running:false, link:'', qr:'', __offline:true
    };
    S.version = d.version;
    applyTrial(d.trial);
    const vc = $('#verChip'); if (vc) vc.textContent = 'v' + d.version;
    const fr = $('#footRight'); if (fr) fr.innerHTML = 'Windows · <b>v' + d.version + '</b>';
    return d;
  }

  async function backendOfflineUI(){
    S.backendOk = false;
    S.engineOk = false;
    resetChecks();
    IDS.forEach(id => setCheck(id, 'warn', '等待启动器后端…', '连接中'));
    const st = $('#stageTitle'); if (st) st.textContent = '后端还在启动…';
    const ss = $('#stageSub'); if (ss) ss.textContent = '界面已经打开；点「重新检测」重试一次，不用关窗口';
    const bt = $('#startTxt'); if (bt) bt.textContent = '重新连接';
  }

  async function bootSequence(recheckOnly){
    if (S.booting) return;
    S.booting = true;
    try {
    const slowHint = setTimeout(() => {
      if (!api()) { const m = $('#bootMsg'); if (m) m.textContent = '后端启动较慢，正在继续等待…'; }
    }, 8000);
    const hasApi = DEMO ? false : await waitApi(12000);
    clearTimeout(slowHint);
    if (!hasApi && DEMO) {
      const m = $('#bootMsg'); if (m) m.textContent = '演示模式 · 正在准备界面';
    } else if (!hasApi) {
      const m = $('#bootMsg'); if (m) m.textContent = '等待本机后端连接…';
    }
    const minBoot = new Promise(r => setTimeout(r, (SKIPBOOT || recheckOnly) ? 0 : 2050));
    let d = null;
    if (hasApi) {
      for (let i = 0; i < 3 && !d; i++) {
        try { d = await bootData(); } catch (e) { d = null; }
        if (!d) { bootPayload = null; await sleep(700); }
      }
    }
    if (!d) d = DEMO ? demoBoot() : offlineBoot();
    await minBoot;
    bootDone();

    if (d.__offline) {
      await backendOfflineUI();
      return;
    }

    if (d.running) {
      await runChecks();
      if (d.link) showQr(d.link, d.qr);
      return;
    }
    if (DEMO === 'ready') {
      await runChecks();
      showQr('https://an.example.invalid/?k=demo', 'demo-qr.svg');
      return;
    }
    await runChecks();
    if (S.engineOk) setTimeout(start, 650);
    } catch (err) {
      apiLog('boot failed: ' + String((err && err.message) || err || 'unknown'));
      bootDone();
      await backendOfflineUI();
      const st = $('#stageTitle'); if (st) st.textContent = '启动器连接异常';
      const ss = $('#stageSub'); if (ss) ss.textContent = '界面已经打开；点「重新检测」重试，不要重复双击启动。';
      const bt = $('#startTxt'); if (bt) bt.textContent = '重新连接';
    } finally {
      S.booting = false;
    }
  }

  bootSequence();
})();
