let days=30,statusFilter='',search='',searchTimer=null,refreshing=false;
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusLabels={triggered:'Getriggert',hardlinked:'Hardlink',queued:'Queued',processing:'Processing',uploaded:'Hochgeladen',filtered:'Gefiltert',dupe:'Dupe',failed:'Fehler',skipped_tracker:'Skip: Zieltracker',skipped_group:'Skip: Group',skipped_category:'Skip: Kategorie',skipped_tag:'Skip: Tag',skipped_existing:'Skip: Ziel existiert'};

/* ---------- Theme ---------- */
const THEME_KEY='uppollo-stats-theme';
function resolvedTheme(mode){return mode==='system'?(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'):mode}
function setTheme(mode,persist=true){
  if(!['system','light','dark'].includes(mode))mode='system';
  document.documentElement.dataset.theme=resolvedTheme(mode);
  document.documentElement.dataset.themeMode=mode;
  if(persist)localStorage.setItem(THEME_KEY,mode);
  document.querySelectorAll('.theme-btn').forEach(b=>b.classList.toggle('active',b.dataset.theme===mode));
}
function initTheme(){
  setTheme(localStorage.getItem(THEME_KEY)||'system',false);
  document.querySelectorAll('.theme-btn').forEach(b=>b.addEventListener('click',()=>setTheme(b.dataset.theme)));
  matchMedia('(prefers-color-scheme: light)').addEventListener?.('change',()=>{if((localStorage.getItem(THEME_KEY)||'system')==='system')setTheme('system',false)});
}

function chipClass(s){return String(s||'').startsWith('skipped_')?'skip':s}
function fmtTime(s){
  if(!s)return '–';
  const d=new Date(s);
  return new Intl.DateTimeFormat('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(d);
}
async function getJSON(url){
  const r=await fetch(url,{cache:'no-store'});
  if(!r.ok)throw new Error(await r.text());
  return r.json();
}

/* ---------- Charts ---------- */
function renderActivity(rows){
  const el=$('#activityChart');
  if(!rows.length){el.innerHTML='<div class="empty">Keine Daten</div>';return}
  const max=Math.max(...rows.flatMap(r=>[Number(r.triggered)||0,Number(r.uploaded)||0]),1);
  const labelEvery=rows.length>45?7:rows.length>20?3:rows.length>12?2:1;
  el.innerHTML=rows.map((r,i)=>{
    const triggered=Number(r.triggered)||0,uploaded=Number(r.uploaded)||0;
    const th=triggered?Math.max(3,triggered/max*86):1.5;
    const uh=uploaded?Math.max(3,uploaded/max*86):1.5;
    const label=new Date(r.day+'T12:00:00').toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'});
    const shown=(i%labelEvery===0||i===rows.length-1)?label:'';
    return `<div class="chart-day" data-tip="${label}: ${triggered} Trigger · ${uploaded} Uploads"><div class="chart-bar triggered" style="height:${th}%"></div><div class="chart-bar uploaded" style="height:${uh}%"></div><div class="chart-label">${shown}</div></div>`;
  }).join('');
}
function renderCats(rows){
  const el=$('#categoryBars');
  const max=Math.max(...rows.map(r=>Number(r.n)||0),1);
  el.innerHTML=rows.length?rows.map(r=>`<div class="bar-row"><div class="bar-name" title="${esc(r.category||'Unbekannt')}">${esc(r.category||'Unbekannt')}</div><div class="bar-track"><div class="bar-fill" style="width:${(Number(r.n)||0)/max*100}%"></div></div><div class="bar-n">${Number(r.n)||0}</div></div>`).join(''):'<div class="empty">Keine Daten</div>';
}

/* ---------- Data ---------- */
async function loadStats(){
  const d=await getJSON(`/api/stats?days=${days}`),s=d.summary;
  $('#kTriggered').textContent=s.triggered??0;
  $('#kUploaded').textContent=s.uploaded??0;
  $('#kRate').textContent=`${Number(s.success_rate||0).toLocaleString('de-DE')} %`;
  $('#kFiltered').textContent=(Number(s.filtered)||0)+(Number(s.dupe)||0);
  $('#kSkipped').textContent=s.skipped??0;
  $('#kFailed').textContent=s.failed??0;
  $('#kActive').textContent=s.active??0;
  $('#kPeriod').textContent=`${days} Tage`;
  renderActivity(d.daily||[]);
  renderCats(d.categories||[]);
}
async function loadHistory(){
  const p=new URLSearchParams({days:String(days),limit:'300'});
  if(statusFilter)p.set('status',statusFilter);
  if(search)p.set('q',search);
  const d=await getJSON('/api/uploads?'+p);
  const body=$('#historyBody'),empty=$('#emptyState'),items=d.items||[];
  body.innerHTML=items.map(x=>`<tr><td class="time">${fmtTime(x.triggered_at)}</td><td class="release">${esc(x.release_name)}</td><td>${esc(x.category||'–')}</td><td>${esc(x.release_group||'–')}</td><td><span class="chip ${chipClass(x.status)}">${esc(statusLabels[x.status]||x.status)}</span></td><td class="reason">${esc(x.reason||'–')}</td></tr>`).join('');
  empty.classList.toggle('hidden',items.length>0);
}
async function health(){
  const box=$('#syncStatus'),text=$('#healthText');
  try{
    const h=await getJSON('/api/health');
    box.classList.remove('error');
    if(!refreshing)box.classList.remove('refreshing');
    text.textContent=h.log_dir_exists?`Online · v${h.version||'–'} · upPollo-Logs verbunden`:`Online · v${h.version||'–'} · Log-Verzeichnis fehlt`;
  }catch(e){
    box.classList.remove('refreshing');
    box.classList.add('error');
    text.textContent='Stats nicht erreichbar';
    throw e;
  }
}
async function refresh(){
  if(refreshing)return;
  refreshing=true;
  const box=$('#syncStatus'),btn=$('#refreshBtn');
  box.classList.remove('error');box.classList.add('refreshing');
  $('#healthText').textContent='Aktualisierung läuft';
  btn.disabled=true;
  try{
    await Promise.all([loadStats(),loadHistory(),health()]);
  }catch(e){
    console.error(e);
    box.classList.remove('refreshing');box.classList.add('error');
    $('#healthText').textContent='Fehler beim Aktualisieren';
  }finally{
    refreshing=false;
    btn.disabled=false;
    try{await health()}catch(e){}
  }
}

/* ---------- Events ---------- */
$('#daysControl').addEventListener('click',e=>{
  const value=e.target.dataset.days;
  if(!value)return;
  days=Number(value);
  document.querySelectorAll('#daysControl button').forEach(b=>b.classList.toggle('active',b===e.target));
  refresh();
});
$('#refreshBtn').addEventListener('click',refresh);
$('#statusFilter').addEventListener('change',e=>{statusFilter=e.target.value;loadHistory().catch(console.error)});
$('#searchInput').addEventListener('input',e=>{
  clearTimeout(searchTimer);
  searchTimer=setTimeout(()=>{search=e.target.value.trim();loadHistory().catch(console.error)},250);
});

initTheme();
refresh();
setInterval(refresh,10000);
