const $=id=>document.getElementById(id)

function api(path,opts,ms){
  ms=ms||5000
  return new Promise(resolve=>{
    const c=new AbortController()
    const t=setTimeout(()=>c.abort(),ms)
    fetch(path,{...(opts||{}),signal:c.signal})
      .then(r=>{clearTimeout(t);if(!r.ok)throw new Error(r.statusText);return r.json()})
      .then(d=>resolve(d))
      .catch(e=>resolve({error:e.name==='AbortError'?'timeout':e.message}))
  })
}

function toggleTheme(){
  const d=document.documentElement
  const n=d.getAttribute('data-theme')==='light'?'dark':'light'
  d.setAttribute('data-theme',n)
  localStorage.setItem('theme',n)
  $('tBtn').textContent=n==='dark'?'🌙':'☀️'
}
(function(){
  const s=localStorage.getItem('theme')||'dark'
  document.documentElement.setAttribute('data-theme',s)
  $('tBtn').textContent=s==='dark'?'🌙':'☀️'
})()

document.querySelectorAll('.nav button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'))
    b.classList.add('active')
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'))
    const id=b.dataset.tab==='api'?'tab-api':'tab-'+b.dataset.tab
    $(id).classList.add('active')
  }
})

function ft(ts){return ts?new Date(ts).toLocaleString():'—'}
function rd(d){
  if(d==null)return''
  const c=d>0?'up':d<0?'dn':'zr',a=d>0?'↑':d<0?'↓':'→'
  return' <span class="'+c+'">'+a+(d>0?'+':'')+d.toFixed(1)+'</span>'
}
function ri(d){
  if(d==null)return''
  const c=d>0?'up':d<0?'dn':'zr',a=d>0?'↑':d<0?'↓':'→'
  return' <span class="'+c+'">'+a+(d>0?'+':'')+d+'</span>'
}

function rt(ts){
  if(!ts||!ts.length)return'<div class="empty">No targets</div>'
  return ts.map(t=>{
    const cpu=(t.cpu||[]).map(m=>'<span>CPU <span class="v">'+m.period+':</span> '+Number(m.value).toFixed(1)+'%'+rd(m.diff)+'</span>').join('')
    const mem=(t.memory||[]).map(m=>'<span>Mem <span class="v">'+m.period+':</span> '+Number(m.value).toFixed(1)+'%'+rd(m.diff)+'</span>').join('')
    const disk=(t.disks||[]).map(d=>(d.metrics||[]).map(m=>'<span>Disk '+(d.mountpoint==='/'?'root':d.mountpoint)+' <span class="v">'+m.period+':</span> '+Number(m.value).toFixed(1)+'%'+rd(m.diff)+'</span>').join('')).join('')
    const oom=(t.oom||[]).map(o=>'<span style="color:var(--red)">💀 OOM <span class="v">'+o.period+':</span> '+o.count+' kill(s)'+ri(o.diff)+'</span>').join('')
    return'<div class="target"><h3>'+t.name+'</h3><div class="row">'+cpu+mem+disk+oom+'</div></div>'
  }).join('')
}

function rhi(r,i,a){
  return'<div class="hi'+(a?' on':'')+'" onclick="showReport('+i+')">'+
    '<span class="t">'+ft(r.timestamp)+'</span>'+
    '<span class="b">'+(r.targets||[]).length+' targets</span></div>'
}

function showReport(i){
  const rs=window._reports
  if(!rs||!rs[i])return
  const r=rs[i]
  $('hd').innerHTML=rt(r.targets)+'<div class="ts">'+ft(r.timestamp)+' #'+(i+1)+'</div>'
  document.querySelectorAll('.hi').forEach(x=>x.classList.remove('on'))
  const items=document.querySelectorAll('.hi')
  if(items[rs.length-1-i])items[rs.length-1-i].classList.add('on')
}

window._reports=[]

async function refresh(){
  // health — 5s timeout, show OK/NOT OK
  const h=await fetch('/healthcheck',{signal:AbortSignal.timeout(5000)}).then(r=>r.text()).catch(()=>'')
  const b=$('hBadge')
  if(h==='ItsOK'){b.textContent='✓ OK';b.className='badge ok'}
  else{b.textContent='✗ NOT OK';b.className='badge fail'}

  // data — 5s each, isolated failures
  const rr=await Promise.all([
    api('/api/status'),api('/api/reports'),api('/api/config'),api('/api/scheduler'),api('/api/notifications')
  ])
  const[sr,reports,cfg,sched,notif]=rr

  $('sub').textContent=new Date().toLocaleTimeString()

  // Dashboard
  $('lr').innerHTML=sr.error?'<div class="empty">'+sr.error+'</div>':rt(sr.targets)+'<div class="ts">'+ft(sr.timestamp)+'</div>'

  // History
  window._reports=reports.error?[]:reports
  const hl=$('hl')
  if(!window._reports.length){hl.innerHTML='<div class="empty">No history</div>';$('hd').innerHTML='<div class="empty">Select a report</div>'}
  else{
    hl.innerHTML=window._reports.slice().reverse().map((r,i)=>rhi(r,window._reports.length-1-i,i===0)).join('')
    showReport(window._reports.length-1)
  }
  $('st').textContent=window._reports.length+' reports'

  // Config
  $('cv').textContent=cfg.error?cfg.error:JSON.stringify(cfg,null,2)

  // Scheduler
  const se=$('sv')
  if(sched.error)se.innerHTML='<div class="empty">'+sched.error+'</div>'
  else{
    const js=s=>s?'<div class="sr"><span class="l">Last run</span><span class="v">'+ft(s.last_run)+'</span></div>'+
      '<div class="sr"><span class="l">Result</span><span class="v"><span class="tag '+(s.last_success?'ok':'fail')+'">'+(s.last_success?'OK':(s.last_error||'FAIL'))+'</span></span></div>':'—'
    se.innerHTML='<div style="margin-bottom:12px"><div style="font-size:12px;color:var(--text2);margin-bottom:4px">Analyze: <code>'+sched.analyze_cron+'</code></div>'+js(sched.status.analyze)+'</div>'+
      '<div><div style="font-size:12px;color:var(--text2);margin-bottom:4px">Send: <code>'+sched.send_cron+'</code></div>'+js(sched.status.send)+'</div>'
  }

  // Notifications
  const ne=$('nv')
  if(notif.error)ne.innerHTML='<div class="empty">'+notif.error+'</div>'
  else if(!notif.length)ne.innerHTML='<div class="empty">No notifications sent yet</div>'
  else ne.innerHTML=notif.slice().reverse().map(n=>
    '<div class="ni"><div class="top"><span class="tm">'+ft(n.timestamp)+'</span>'+
    '<span class="tag '+(n.success?'ok':'fail')+'">'+(n.success?'✓ Delivered':'✗ Failed')+'</span></div>'+
    (n.chat_id?'<div style="font-size:11px;color:var(--text2)">Chat: '+n.chat_id+'</div>':'')+
    (n.error?'<div style="font-size:11px;color:var(--red)">'+n.error+'</div>':'')+'</div>'
  ).join('')

  // Auto-test VM + Clouds every 30s (result only updates console tab)
  autoCheck()
}

let _lastVmCheck=0,_lastCloudCheck=0,_vmOk=null,_cloudOk=null
function autoCheck(){
  const now=Date.now()
  if(now-_lastVmCheck>30000){
    _lastVmCheck=now
    api('/api/test/vm',{},5000).then(r=>{_vmOk=r.success})
  }
  if(now-_lastCloudCheck>30000){
    _lastCloudCheck=now
    api('/api/test/clouds',{},5000).then(r=>{_cloudOk=r.success})
  }
}

// Console tab — show latest known status on tab switch
document.querySelectorAll('.nav button').forEach(b=>{
  const orig=b.onclick
  b.onclick=()=>{
    orig()
    if(b.dataset.tab==='console'){
      $('vmr').innerHTML=_vmOk===null?'<span class="info">not tested</span>':
        '<span class="'+( _vmOk?'ok':'fail')+'">'+(_vmOk?'✓ OK':'✗ NOT OK')+'</span>'
      $('cr').innerHTML=_cloudOk===null?'<span class="info">not tested</span>':
        '<span class="'+( _cloudOk?'ok':'fail')+'">'+(_cloudOk?'✓ OK':'✗ NOT OK')+'</span>'
    }
  }
})

async function loadPreview(){
  const r=await api('/api/preview',{},5000)
  $('pv').textContent=r.error?r.error:r.text
}

// console tests
function testVM(){
  const el=$('vmr')
  el.innerHTML='<span class="info">…</span>'
  api('/api/test/vm',{},5000).then(r=>{
    _vmOk=r.success
    el.innerHTML=r.success?'<span class="ok">✓ OK</span>':'<span class="fail">✗ NOT OK — '+(r.error||'no response')+'</span>'
  })
}
function testClouds(){
  const el=$('cr')
  el.innerHTML='<span class="info">…</span>'
  api('/api/test/clouds',{},5000).then(r=>{
    _cloudOk=r.success
    if(r.success)el.innerHTML='<span class="ok">✓ OK</span><div class="info" style="margin-top:4px">API: '+r.api_url+'</div><div class="info">Chat: '+r.chat_id+'</div>'
    else el.innerHTML='<span class="fail">✗ NOT OK — '+(r.error||'no response')+'</span>'
  })
}
function testSend(){
  const el=$('sr')
  el.innerHTML='<span class="info">…</span>'
  api('/api/test/send',{method:'POST'},5000).then(r=>{
    el.innerHTML=r.success?'<span class="ok">✓ Sent</span>':'<span class="fail">✗ '+(r.error||'no response')+'</span>'
  })
}

function ta(){api('/api/analyze',{method:'POST'},15000).then(refresh)}
function cl(){if(confirm('Clear all reports?'))api('/api/clear',{method:'POST'},5000).then(refresh)}

refresh()
setInterval(refresh,30000)
