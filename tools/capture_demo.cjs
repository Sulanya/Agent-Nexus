const fs=require('node:fs'),path=require('node:path');
const [debugPort,baseURL,out]=process.argv.slice(2);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function main(){
  let targets;
  for(let i=0;i<50;i++){
    try{targets=await (await fetch('http://127.0.0.1:'+debugPort+'/json/list')).json();if(targets.some(t=>t.type==='page'))break;}catch(e){}
    await sleep(150);
  }
  const target=targets.find(t=>t.type==='page'),ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
  let counter=0;const pending=new Map(),exceptions=[];
  ws.onmessage=message=>{const d=JSON.parse(message.data);if(d.id){const p=pending.get(d.id);pending.delete(d.id);if(d.error)p.reject(new Error(d.error.message));else p.resolve(d.result);}else if(d.method==='Runtime.exceptionThrown')exceptions.push(d.params.exceptionDetails.text);};
  function call(method,params={}){const id=++counter;return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
  async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
  await call('Page.enable');await call('Runtime.enable');
  try{
    for(const [name,width,height,mobile] of [['desktop',1440,960,false],['tablet',1024,1100,false],['mobile',430,932,true]]){
      await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile,screenWidth:width,screenHeight:height});
      await call('Page.navigate',{url:baseURL+'/?open=demo-0'});
      let state;
      for(let i=0;i<100;i++){
        try{state=await evaluate(`({online:document.querySelector('#conn')?.className,text:document.querySelector('#connTxt')?.textContent,loaded:!!document.querySelector('#chatView .msg')})`);if(state.online==='on'&&state.loaded)break;}catch(e){}
        await sleep(100);
      }
      if(state?.online!=='on'||!state.loaded)throw new Error(name+' did not reach a connected rendered conversation: '+JSON.stringify(state));
      await sleep(3400);
      await evaluate(`(()=>{const c=document.querySelector('#chatView');c.scrollTop=0;return true})()`);
      state=await evaluate(`({online:document.querySelector('#conn')?.className,text:document.querySelector('#connTxt')?.textContent,bodyWidth:document.documentElement.clientWidth,overflow:document.documentElement.scrollWidth>innerWidth,brand:document.querySelector('#brand')?.className})`);
      if(state.online!=='on'||state.overflow)throw new Error('Invalid screenshot state '+JSON.stringify(state));
      const capture=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,fromSurface:true});
      fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(capture.data,'base64'));
      console.log(name+' '+JSON.stringify(state));
    }
    if(exceptions.length)throw new Error('Frontend exceptions: '+JSON.stringify(exceptions));
  }finally{ws.close();}
}
main().catch(e=>{console.error(e.message);process.exitCode=1});
