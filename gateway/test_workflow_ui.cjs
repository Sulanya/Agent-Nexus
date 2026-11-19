const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const uiPath = path.join(__dirname, '..', 'ui', 'index.html');
const source = fs.readFileSync(uiPath, 'utf8');
const sidebarStart=source.indexOf('(function setupSidebarResize(){');
const sidebarEnd=source.indexOf('</script>',sidebarStart);
const sidebarSource=source.slice(sidebarStart,sidebarEnd);
const start = source.indexOf('function workflowEngine(t)');
const end = source.indexOf('async function threadProjectSheet(t)', start);
if (start < 0 || end < 0) throw new Error('workflow UI source block not found');

const helpers = `
const S={viewonly:false};
let SHEET_SEQ=0,SHEET_OPTS=null;
const calls=[],submissions=[];
let apiFailureCount=1,showUnknown=true,healthVersion=2;
const sessionValues=new Map();
Object.defineProperty(window,'sessionStorage',{configurable:true,value:{
  getItem:key=>sessionValues.get(key)||null,
  setItem:(key,value)=>sessionValues.set(key,String(value)),
  removeItem:key=>sessionValues.delete(key)
}});
function $(selector){return document.querySelector(selector)}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n}
function ic(_name,cls,tag){return el(tag||'span',cls)}
function threadTitle(t){return t.name||t.preview||t.id||'(empty)'}
function apiErrText(e,label){return (e&&e.message)||label}
function toast(){}
function closeSheet(){document.querySelector('#sheetWrap').classList.remove('show')}
function showSheet(){document.querySelector('#sheetWrap').classList.add('show')}
function anchorSheet(){}
function sheetRenderItems(body,items){(items||[]).forEach(item=>body.appendChild(el('div','sh-item',item.label||item.loading||'')))}
function openSheet(title,items){
  SHEET_SEQ++;const sheet=$('#sheet');sheet.innerHTML='';sheet.appendChild(el('div','sh-head',title));sheet.appendChild(el('div','sh-body'));
  sheetRenderItems(sheet.querySelector('.sh-body'),items);showSheet();return SHEET_SEQ;
}
async function api(url,opts={}){
  calls.push({url,opts});
  if(url==='/api/health')return {workflowVersion:healthVersion};
  if(url.startsWith('/api/threads?engine=')){
    const engine=new URL('http://local'+url).searchParams.get('engine');
    return {data:[{id:'target-'+engine,name:'Target '+engine,engine}]};
  }
  if(url==='/api/workflows'&&opts.method==='POST'){
    submissions.push(JSON.parse(JSON.stringify(opts.body)));
    if(apiFailureCount-->0)throw new Error('simulated timeout');
    return {data:{id:'created-task'}};
  }
  if(url.startsWith('/api/workflows?')){
    return {data:showUnknown?[{id:'task-1',title:'Review result',target_engine:'codex',target_thread_id:'source-1',run_at:1800000000,kind:'scheduled',state:'needs_review',outcomeUnknown:true,last_error:'result unknown'}]:[]};
  }
  if(url.includes('/reconcile')||url.includes('/complete'))return {task:{id:'task-1'}};
  throw new Error('Unexpected API request '+url);
}
`;

const checks = `
(async()=>{
  try{
    const resize=document.querySelector('#sidebarResize');
    if(!resize||!resize.hidden)throw new Error('sidebar resize is not hidden on mobile');
    Object.defineProperty(window,'innerWidth',{configurable:true,value:1280});
    window.dispatchEvent(new Event('resize'));
    if(resize.hidden)throw new Error('sidebar resize unavailable on desktop');
    resize.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
    if(document.documentElement.style.getPropertyValue('--sidebar-width')!=='320px')throw new Error('sidebar keyboard resize failed');
    resize.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));
    if(document.documentElement.style.getPropertyValue('--sidebar-width')!=='300px')throw new Error('sidebar reset failed');
    const sourceThread={id:'source-1',engine:'codex',name:'Source conversation'};
    await workflowCompose(sourceThread,'scheduled');
    const time=$('#sheet input[type="datetime-local"]');
    const instruction=$('#sheet textarea');
    time.value=workflowLocalValue(new Date(Date.now()+60*60000));
    instruction.value='Run focused tests and report failures';
    const submit=$('#sheet .wf-submit');
    await submit.onclick();
    if(!$('#sheet .wf-submit'))throw new Error('submission sheet disappeared after timeout');
    if(!$('#sheet textarea').disabled)throw new Error('uncertain form remained editable');
    await workflowCompose(sourceThread,'scheduled');
    const retrySubmit=$('#sheet .wf-submit');
    if(!$('#sheet textarea').disabled)throw new Error('pending request was not restored as locked');
    await retrySubmit.onclick();
    if(submissions.length!==2)throw new Error('network retry did not submit twice');
    if(submissions[0].requestId!==submissions[1].requestId)throw new Error('network retry changed requestId');
    if(JSON.stringify(submissions[0])!==JSON.stringify(submissions[1]))throw new Error('network retry changed payload');
    if(submissions[0].targetThreadId!=='source-1'||submissions[0].targetEngine!=='codex')throw new Error('scheduled target mismatch');
    if(submissions[0].runAt!==new Date(time.value).toISOString())throw new Error('local time was not converted to ISO');

    await workflowCompose(sourceThread,'handoff');
    const target=$('#sheet select');
    target.value='claude\\ttarget-claude';
    $('#sheet textarea').value='Continue with the API audit';
    await $('#sheet .wf-submit').onclick();
    const handoff=submissions[2];
    if(handoff.kind!=='handoff'||handoff.targetEngine!=='claude'||handoff.targetThreadId!=='target-claude')throw new Error('handoff destination mismatch');
    if(handoff.sourceThreadId!=='source-1'||handoff.instruction!=='Continue with the API audit')throw new Error('handoff source/instruction mismatch');

    await workflowManager(sourceThread);
    const labels=[...document.querySelectorAll('.wf-row-actions button')].map(x=>x.textContent);
    if(!labels.includes('继续核对原请求')||!labels.includes('确认已完成'))throw new Error('unknown-result actions missing');
    const retryButton=[...document.querySelectorAll('.wf-row-actions button')].find(x=>x.textContent==='继续核对原请求');
    retryButton.click();
    await new Promise(resolve=>setTimeout(resolve,0));
    if(!calls.some(x=>x.url.endsWith('/reconcile')))throw new Error('manual reconcile action missing');
    healthVersion=1;WORKFLOW_CAPABILITY.checkedAt=0;
    if(await workflowBackendReady())throw new Error('old backend version was incorrectly enabled');
    healthVersion=2;WORKFLOW_CAPABILITY.checkedAt=0;
    if(!await workflowBackendReady())throw new Error('current backend version was not enabled');
    document.querySelector('#test-result').textContent='PASS';
  }catch(error){document.querySelector('#test-result').textContent='FAIL: '+error.stack;}
})();
`;

const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;font:14px system-ui}#sheetWrap{display:block}#sheet{width:min(94vw,460px);max-height:90vh;overflow:auto;border:1px solid #ccc}.wf-form{display:grid;gap:10px;padding:12px}.wf-field{display:grid;gap:4px}.wf-field input,.wf-field textarea,.wf-field select{width:100%;box-sizing:border-box}.wf-row{padding:8px}.wf-row-actions{display:flex;gap:8px}</style>
<div id="sheetWrap" class="show"><section id="sheet"></section></div><pre id="test-result">RUNNING</pre>
<script>${helpers}</script><script>${sidebarSource}</script><script>${source.slice(start, end)}</script><script>${checks}</script>`;

const edge = process.env.AN_EDGE_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const profile = path.join(os.tmpdir(), 'an-workflow-ui-test-' + process.pid);
try {
  const result = spawnSync(edge, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--no-sandbox', '--virtual-time-budget=5000',
    '--window-size=375,667', '--dump-dom', '--user-data-dir=' + profile,
    'data:text/html;base64,' + Buffer.from(html).toString('base64')
  ], { encoding: 'utf8', timeout: 20000, maxBuffer: 4 * 1024 * 1024 });
  const output = result.stdout || '';
  const match = output.match(/<pre id="test-result">([\s\S]*?)<\/pre>/);
  const status = match ? match[1].replace(/<[^>]+>/g, '').replace(/&quot;/g, '"') : '';
  if (result.error) throw result.error;
  if (result.status !== 0 || !status.startsWith('PASS')) {
    throw new Error((status || 'Edge harness did not report completion').slice(0,700));
  }
  process.stdout.write('Workflow UI smoke passed at 375x667\n');
} finally {
  fs.rmSync(profile, { recursive: true, force: true });
}
