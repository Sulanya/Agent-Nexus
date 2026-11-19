"""Recover a missing browser login without weakening gateway authentication."""
import html


AUTH_BOOTSTRAP_JS = r"""const TOKEN = (function(){
  const params=new URLSearchParams(location.search);
  let key=params.get('k')||'';
  if(!key){
    try{const cookie=document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('arc='));
      if(cookie)key=decodeURIComponent(cookie.slice(4));}catch(e){}
  }
  if(!key){try{key=localStorage.getItem('arcToken')||'';}catch(e){}}
  if(key){
    try{localStorage.setItem('arcToken',key);}catch(e){}
    try{document.cookie='arc='+encodeURIComponent(key)+'; path=/; max-age=31536000; SameSite=Lax'+(location.protocol==='https:'?'; Secure':'');}catch(e){}
  }
  if(params.has('k')){
    params.delete('k');
    const query=params.toString();
    try{history.replaceState(history.state,'',location.pathname+(query?'?'+query:'')+location.hash);}catch(e){}
  }
  return key;
})();"""


def login_page(has_credential=False):
    title='当前登录信息未通过校验' if has_credential else '登录你的 AN 主实例'
    explanation=('这份连接信息无法用于当前实例。服务仍在运行；不需要先重启。' if has_credential else
                 '这个地址没有携带登录信息。更换临时域名后，浏览器不会自动继承旧域名的登录状态。')
    return PAGE.replace('__TITLE__',html.escape(title)).replace('__EXPLANATION__',html.escape(explanation))


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>AN · 登录恢复</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c101a;color:#f2f3ff;font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}.card{width:min(480px,calc(100% - 32px));margin:24px 0;padding:30px;background:#171d30;border:1px solid #343e66;border-radius:24px}svg{width:52px;height:52px}.brand{display:flex;align-items:center;gap:12px;font-size:13px;letter-spacing:.08em;color:#c4cae4}h1{font-size:26px;line-height:1.35;margin:22px 0 14px}p{color:#c3c8dd;margin:14px 0}label{display:block;margin:22px 0 8px;font-size:14px}input,button{font:inherit;border-radius:12px;width:100%;min-height:48px}input{padding:12px;background:#0c1222;border:1px solid #515d89;color:#fff}input:focus{outline:2px solid #a5a1ff;outline-offset:2px}button{margin-top:14px;padding:11px;border:0;background:#6960e7;color:white;cursor:pointer}button:disabled{opacity:.65;cursor:wait}.note{font-size:13px;color:#a3afcc;border-top:1px solid #343e66;padding-top:18px;margin-top:24px}.status{min-height:24px;font-size:14px;color:#a8def1}details{margin-top:20px}summary{cursor:pointer;color:#d5d8ed}code{font-size:12px;overflow-wrap:anywhere}.error{color:#ffb8c9}@media(prefers-color-scheme:light){:root{color-scheme:light}body{background:#f2f4fb;color:#172141}.card{background:#fff;border-color:#d1d7eb}p{color:#4c597d}input{background:#f7f8fe;color:#182244;border-color:#aab5d7}.note,summary{color:#596786}.status{color:#216477}}
</style></head><body><main class="card">
<div class="brand"><svg viewBox="0 0 48 48" aria-hidden="true"><defs><linearGradient id="brand"><stop stop-color="#a893ff"/><stop offset=".55" stop-color="#7775f4"/><stop offset="1" stop-color="#57d5e7"/></linearGradient></defs><path d="M35 11C19 1 5 19 24 24s5 23-11 13" fill="none" stroke="url(#brand)" stroke-width="4" stroke-linecap="round"/><circle cx="35" cy="11" r="3.5" fill="#a893ff"/><circle cx="13" cy="37" r="3.5" fill="#57d5e7"/></svg>AGENT NEXUS · 连接恢复</div>
<h1>__TITLE__</h1><p>__EXPLANATION__</p>
<form id="login"><label for="credential">当前连接链接或访问码</label><input id="credential" name="credential" type="password" autocomplete="off" autocapitalize="none" spellcheck="false" required placeholder="从自己的电脑获取，不要发送到聊天里"><button id="connect" type="submit">连接我的实例</button></form>
<p class="status" id="status" role="status" aria-live="polite"></p>
<details><summary>我是开发者，在自己的电脑上恢复</summary><p>运行 AN 目录里的 <code>tools\open-owner.vbs</code>，会打开当前有效的本机入口和手机连接码。</p><p>也可使用已生成的 <code>_run\OWNER-CONNECT.html</code>。无需停止服务或重新创建凭证。</p></details>
<p class="note">本页是登录恢复入口，不代表开发权限、订阅或试用到期。外网访问仍需你自己实例的有效凭证。</p>
<script>
(()=>{
 const field=document.getElementById('credential'),button=document.getElementById('connect'),status=document.getElementById('status');
 let busy=false;
 function keyFrom(value){
   value=String(value||'').trim();if(!value)return '';
   if(/^https?:\/\//i.test(value)){const url=new URL(value);return url.searchParams.get('k')||'';}
   return value;
 }
 async function connect(value,automatic){
   if(busy)return;let key='';
   try{key=keyFrom(value);}catch(e){status.textContent='链接格式不正确，请粘贴完整连接链接或访问码。';return;}
   if(!key){status.textContent='连接链接中缺少访问码，请从自己的电脑重新获取。';return;}
   busy=true;button.disabled=true;status.classList.remove('error');status.textContent=automatic?'正在恢复这个浏览器的登录…':'正在核对当前实例…';
   const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),10000);
   try{
     const response=await fetch('/api/whoami',{headers:{'X-Auth':key},cache:'no-store',signal:ctl.signal});
     if(response.status===401)throw Error('这份连接信息未通过校验，请使用当前实例的手机连接码。');
     if(!response.ok)throw Error('服务暂时未能完成校验，请稍后重试。');
     const result=await response.json();if(!['full','view'].includes(result.role))throw Error('登录状态尚未确认，请重试。');
     try{localStorage.setItem('arcToken',key);}catch(e){}
     document.cookie='arc='+encodeURIComponent(key)+'; path=/; max-age=31536000; SameSite=Lax'+(location.protocol==='https:'?'; Secure':'');
     // The query is needed when storage/cookies are unavailable. The UI clears
     // it from the address bar immediately after bootstrap. Never navigate to
     // a pasted origin, only the current gateway.
     location.replace('/?k='+encodeURIComponent(key));
   }catch(error){status.classList.add('error');status.textContent=error.name==='AbortError'?'连接超时，请检查电脑是否在线后重试。':error.message;}
   finally{clearTimeout(timer);busy=false;button.disabled=false;}
 }
 document.getElementById('login').addEventListener('submit',event=>{event.preventDefault();void connect(field.value,false);});
 let stored='';try{stored=localStorage.getItem('arcToken')||'';}catch(e){}
 const explicit=new URLSearchParams(location.search).get('k')||'';
 if(stored&&stored!==explicit)void connect(stored,true);
})();
</script></main></body></html>"""
