/* 单机工作区：列表上下文独立保存，轮询不重建正在操作的菜单或表单。 */
'use strict';
const el=id=>document.getElementById(id);
const form=el('form');
const state={backendReady:false,tab:'tasks',tasks:[],runs:[],taskPage:1,runPage:1,taskFilter:null,editing:null,dirty:false,loaded:false,busy:false,runPending:false,logRun:null,logRows:[],logPage:1,logTotal:0,logPages:1,logRequest:0};
const names={retry_wait:'等待网络重试',pausing:'正在暂停',paused:'已暂停',running:'执行中',partial:'部分完成',preview_partial:'演练有问题',success:'成功',preview:'演练完成',failed:'失败',interrupted:'已中断'};
const actions={network_retry:'网络等待重试',network_retry_exhausted:'网络重试用尽',control:'执行控制',config_updated:'参数修改',preflight:'目标权限检查',ignored:'规则忽略',conflict:'文件冲突',deletion_skipped:'已暂停删除',mapping_started:'目录组开始',mapping_completed:'目录组完成',mapping_failed:'目录组失败',copied:'复制',skipped:'跳过',deleted:'删除',failed:'文件失败'};
const defaults={name:'',syncStrategy:'AUTO',scanWorkers:1,scannerBackend:'auto',direction:'one_way',conflictPolicy:'skip',ignorePatterns:[],continueOnError:false,networkRetryCount:3,networkRetryMinutes:3,parallelism:4,batchFiles:100,largeThresholdMb:512,bandwidthLimit:0,maxRetries:2,retryInterval:1,comparisonMode:'size',preserveTime:true,timeToleranceSeconds:2,skipUnsupported:true,updateOnly:false,delete:false};
const pageSize=10;
const activeRun=run=>run&&['running','pausing','paused','retry_wait'].includes(run.status);
async function controlRun(run,action){try{await api(`/runs/${run.id}/${action}`,'POST',{});await refresh(true)}catch(error){report(error)}}
async function api(path,method='GET',body){
 if(method!=='GET'&&!state.backendReady)throw Error('后台尚未通过版本检查。请先停止旧单机程序并重新启动，再刷新页面；当前配置未提交。');
 const response=await fetch('/api'+path,{method,cache:'no-store',headers:{'Content-Type':'application/json','X-Sync-Local':'1'},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await response.json();
 if(!response.ok){const detail=data.detail;throw Error(typeof detail==='string'?detail:Array.isArray(detail)?detail.map(x=>`${x.loc.slice(1).join('.')}: ${x.msg}`).join('；'):'请求失败，请重试')}
 return data;
}
function message(text,error=false){const box=el('message');box.replaceChildren();delete box.dataset.kind;box.textContent=text;box.classList.toggle('error',error);box.hidden=false}
function report(error){message(error.message||'操作失败，请重试',true)}
function node(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n}
function cell(row,text){const td=row.insertCell();if(text!==undefined)td.textContent=text;return td}
function button(parent,text,fn,className=''){const b=node('button',text,className);b.type='button';b.onclick=()=>Promise.resolve().then(fn).catch(report);parent.append(b);return b}
function parse(value,fallback={}){try{return JSON.parse(value)||fallback}catch{return fallback}}
function mappingsOf(task){return task.pathMappings||[{source:task.source,target:task.target}]}
function comparisonMode(config){return config.comparisonMode||(config.checksum?'sha256':'size_mtime')}
function comparisonLabel(config){if(config.syncStrategy&&config.syncStrategy!=='AUTO')return {COPY_ALL:'全部覆盖',SKIP_EXISTING:'跳过已有名称',COMPARE_METADATA:'按目录比较大小＋时间',MIRROR:'镜像：大小＋时间及安全删除'}[config.syncStrategy];return {COPY_ALL:'全部覆盖',SKIP_EXISTING:'跳过已有名称',COMPARE_METADATA:'按目录比较大小＋时间',MIRROR:'镜像：大小＋时间',size:'仅文件大小',size_mtime:'大小＋修改时间',sha256:'SHA-256 内容比较＋复制校验'}[comparisonMode(config)]}
function taskName(run){return parse(run.snapshot).name||state.tasks.find(t=>t.id===run.task_id)?.name||'已删除任务'}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—'}
function bytes(value){if(!value)return '0 B';const units=['B','KiB','MiB','GiB','TiB'];const i=Math.min(Math.floor(Math.log(value)/Math.log(1024)),4);return `${(value/1024**i).toFixed(i?1:0)} ${units[i]}`}
function badge(status){return node('span',names[status]||'无近期记录','badge '+(status||''))}
function empty(body,count,text){body.replaceChildren();const c=body.insertRow().insertCell();c.colSpan=count;c.className='empty';c.textContent=text}
function leaveEditor(){return state.tab!=='editor'||!state.dirty||confirm('任务配置尚未保存，确认放弃本次修改？')}
function showTab(tab,force=false){
 if(tab===state.tab)return true;
 if(!force&&!leaveEditor())return false;
 if(state.tab==='editor')state.dirty=false;
 state.tab=tab;el('message').hidden=true;
 for(const item of document.querySelectorAll('[data-tab]')){const selected=item.dataset.tab===tab;item.setAttribute('aria-selected',String(selected));item.tabIndex=selected?0:-1;el('panel-'+item.dataset.tab).hidden=!selected}
 if(tab==='tasks')renderTasks();if(tab==='runs')renderRuns();return true;
}
for(const tab of document.querySelectorAll('[data-tab]'))tab.onclick=()=>showTab(tab.dataset.tab);
el('tab-editor').onclick=()=>{if(state.tab!=='editor')startEditor(state.editing==null?null:state.tasks.find(t=>t.id===state.editing))};
document.querySelector('.tabs').onkeydown=event=>{
 if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
 event.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')].filter(t=>!t.hidden);const index=tabs.indexOf(document.activeElement);const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 tabs[next].click();if(state.tab===tabs[next].dataset.tab)tabs[next].focus();
};
function pager(id,total,key,render){
 const pages=Math.max(1,Math.ceil(total/pageSize));state[key]=Math.min(state[key],pages);
 const box=el(id);box.replaceChildren(node('span',`共 ${total} 条 · 第 ${state[key]} / ${pages} 页`));const controls=node('div');
 button(controls,'上一页',()=>{state[key]--;render()}).disabled=state[key]<=1;
 button(controls,'下一页',()=>{state[key]++;render()}).disabled=state[key]>=pages;box.append(controls);
}
function renderTasks(){
 const q=el('taskSearch').value.trim().toLocaleLowerCase();
 const tasks=state.tasks.filter(t=>[t.name,...mappingsOf(t).flatMap(m=>[m.source,m.target])].join(' ').toLocaleLowerCase().includes(q));
 pager('taskPager',tasks.length,'taskPage',renderTasks);el('taskCount').textContent=state.tasks.length;el('taskSummary').textContent=`${state.tasks.length} 个任务`;
 const body=el('tasks');body.replaceChildren();
 if(!tasks.length){empty(body,5,state.loaded?(q?'没有符合条件的任务，试试其他名称或路径。':'还没有同步任务，点击“新建任务”开始配置。'):'正在加载任务…');return}
 for(const task of tasks.slice((state.taskPage-1)*pageSize,state.taskPage*pageSize)){
  const row=body.insertRow();const title=cell(row);title.append(node('span',task.name,'task-name'));title.append(node('span',`${mappingsOf(task).length} 组目录 · ${task.parallelism??4} 个并发`,'cell-sub'));
  const paths=cell(row);const first=mappingsOf(task)[0];for(const [label,path] of [['源',first.source],['至',first.target]]){const line=node('div',undefined,'path-line');const text=node('span',path,'truncate');text.title=path;line.append(node('span',label,'path-tag'),text);paths.append(line)}
  if(mappingsOf(task).length>1)button(paths,`查看全部 ${mappingsOf(task).length} 组`,()=>startEditor(task),'link');
  const rules=cell(row);rules.append(node('span',task.direction==='bidirectional'?'双向合并 · 不传播删除':task.delete?'单向镜像 · 删除多余文件':'单向增量 · 保留多余文件'));rules.append(node('span',[comparisonLabel(task),task.direction==='bidirectional'?(task.conflictPolicy==='newer'?'较新覆盖':'保留冲突'):task.updateOnly?'保护较新文件':'允许覆盖'].join(' · '),'cell-sub'));
  const recent=state.runs.find(r=>r.task_id===task.id);const status=cell(row);status.append(badge(recent?.status));if(recent)status.append(node('span',date(recent.started),'cell-sub'));
  const ops=node('div',undefined,'row-actions');cell(row).append(ops);if(recent)button(ops,'最近报告',()=>openLogs(recent),'link');
  if(activeRun(recent))button(ops,recent.status==='paused'?'恢复':'暂停',()=>controlRun(recent,recent.status==='paused'?'resume':'pause'),'link').disabled=recent.status==='pausing';
  button(ops,'运行',()=>runTask(task,false),'link').disabled=state.runPending||state.runs.some(r=>activeRun(r));
  const menu=node('details',undefined,'row-menu');const summary=node('summary','更多 ▾');summary.setAttribute('aria-label',`${task.name}的更多操作`);menu.append(summary);const items=node('div',undefined,'menu-items');menu.append(items);
  for(const [label,fn] of [['编辑任务',()=>startEditor(task)],['演练',()=>runTask(task,true)],['复制配置',()=>startEditor(task,true)],['执行记录',()=>{state.taskFilter=task.id;state.runPage=1;el('runSearch').value='';el('runStatus').value='';showTab('runs');renderRuns()}],['删除任务',()=>deleteTask(task)]]){
   const b=button(items,label,()=>{menu.open=false;return fn()},label==='删除任务'?'danger':'');if(label==='演练')b.disabled=state.runPending||state.runs.some(r=>activeRun(r));
  }
  menu.addEventListener('toggle',()=>{if(menu.open)for(const other of document.querySelectorAll('.row-menu[open]'))if(other!==menu)other.open=false});ops.append(menu);
 }
}
// 每两秒展示一次有界快照；进度不依赖逐条读取文件日志。
const phaseNames={checking:'检查目标读写权限',queued:'等待启动',scanning:'扫描目录',planning:'双向比较与规划',syncing:'比较与同步',copying:'写入临时文件',comparing:'比较文件',verifying:'校验写入内容',finalizing:'清理与收尾'};
let lastProgressRefresh=0,progressDisconnected=false;
function progressFreshness(failed=false){
 const box=el('liveProgress');if(box.hidden)return;
 if(failed)progressDisconnected=true;
 const age=Math.floor((Date.now()-lastProgressRefresh)/1000),stale=progressDisconnected||age>=6;
 box.classList.toggle('stale',stale);
 const status=box.querySelector('.activity-freshness');
 if(status)status.textContent=stale?`状态更新中断 · 距上次响应 ${age} 秒；显示的是上次快照`:'● 状态已刷新 · '+new Date(lastProgressRefresh).toLocaleTimeString('zh-CN',{hour12:false});
}
function renderProgress(box,run){
 box.replaceChildren();box.hidden=!activeRun(run);if(box.hidden)return;
 if(run.status==='paused'){box.append(node('strong',run.recovery?.requiresResume?'网络重试已用尽，等待管理员恢复':'已暂停'),node('p',run.recovery?.error||'已有文件已完成处理，可编辑参数或追加目录组，恢复后继续。'));button(box,'恢复任务',()=>controlRun(run,'resume'));return}
 if(run.status==='retry_wait'){const r=run.recovery||{};box.append(node('strong',`等待网络重试 · 第 ${r.attempt??0}/${r.limit??0} 次`),node('p',r.error||'存储连接暂不可用'),node('p',`约 ${Math.max(0,Math.ceil((r.nextRetryAt||0)-Date.now()/1000))} 秒后重试；当前组会重新比较，已完成文件按规则跳过。`));button(box,'立即重试',()=>controlRun(run,'resume'));return}
 const p=run.progress;if(!p){box.append(node('strong','等待后台开始处理…'));return}
 const dry=parse(run.snapshot).dryRun;
 const heading=node('div',undefined,'activity-heading');const spinner=node('span',undefined,'activity-spinner');spinner.setAttribute('aria-hidden','true');heading.append(spinner,node('strong',(run.status==='pausing'?'正在暂停，等待当前文件完成 · ':'正在运行 · ')+taskName(run)),node('span','', 'activity-freshness'));box.append(heading);
 const stage=['queued','checking','scanning','planning'].includes(p.phase)?0:p.phase==='finalizing'?2:1;
 const steps=node('div',undefined,'activity-stages');
 for(const [i,label] of ['扫描 / 规划','比较 / 复制 / 校验','清理 / 收尾'].entries()){const item=node('span',label,i===stage?'current':i<stage?'passed':'');if(i===stage)item.setAttribute('aria-current','step');steps.append(item)}
 box.append(steps);
 const flow=node('div',undefined,'activity-flow');flow.setAttribute('aria-hidden','true');flow.append(node('span'));box.append(flow);
 box.append(node('strong',taskName(run)+' · '+(dry?'演练 · ':'')+(phaseNames[p.phase]||p.phase)));
 box.append(node('p',`目录组 ${p.mappingIndex}/${p.mappingCount} · 已检查 ${p.scannedEntries} 个条目 · 已运行 ${Math.floor(p.elapsedSeconds)} 秒`));
 if(p.phase==='scanning'||p.phase==='planning'){box.append(node('p',`扫描 ${(p.scanEntriesPerSecond??0).toFixed(0)} 条目/秒 · ${p.scanSide==='target'?'目标端':p.scanSide==='source'?'源端':'两端规划'}`));if(p.scanPath)box.append(node('p',p.scanPath,'active-path'))}
 const c=p.counts;
 box.append(node('p',`${dry?'预计':''}复制 ${c.copied} · 跳过 ${c.skipped} · 失败 ${c.failed} · 冲突 ${c.conflicts} · 忽略 ${c.ignored} · 删除 ${c.deleted}`));
 box.append(node('p',`近期处理 ${p.filesPerSecond.toFixed(1)} 文件/秒 · 写入 ${bytes(p.bytesPerSecond)}/秒 · 累计写入 ${bytes(p.writtenBytes)}`));
 if(p.totalFiles!==null&&p.totalFiles>0){
  const done=Math.min(p.finished,p.totalFiles),bar=node('progress');bar.max=p.totalFiles;bar.value=done;bar.setAttribute('aria-label','本目录组文件处理进度');
  box.append(node('span',`本组文件处理：${done}/${p.totalFiles}（${(done/p.totalFiles*100).toFixed(1)}%）；${p.scanComplete?'已完成枚举':'预检总量，目录变化时会校正'}；含跳过和失败，仍需完成收尾。`),bar);
 }else box.append(node('span',`本组已发现 ${p.discovered} 个待处理文件，已处理 ${p.finished} 个；总量未确定，不估算完成率。`,'muted'));
 if(!p.activeFiles.length)box.append(node('p',stage===0?'正在遍历目录并检查条目，总量确定前持续更新扫描计数。':stage===2?'正在处理目标清理和收尾，请等待最终结果。':'正在枚举文件、收集处理结果或等待存储响应；当前没有活动文件快照。','activity-description'));
 for(const file of p.activeFiles.slice(0,4)){
  const item=node('div',undefined,'active-file');
  const label=node('span',(phaseNames[file.phase]||file.phase)+'：'+file.path,'active-path');label.title=file.path;item.append(label);
  if(file.phase==='copying'&&file.size>0){const bar=node('progress');bar.max=file.size;bar.value=Math.min(file.written,file.size);bar.setAttribute('aria-label','当前文件写入进度');item.append(node('span',`${bytes(file.written)} / ${bytes(file.size)}（${Math.min(100,file.written/file.size*100).toFixed(1)}%）`),bar)}
  box.append(item);
 }
 if(p.scanMetrics&&Object.keys(p.scanMetrics).length){const m=p.scanMetrics;box.append(node('p',`源 ${Number(p.sourceScanRate??0).toFixed(0)} / 目标 ${Number(p.targetScanRate??0).toFixed(0)} 条目/秒 · ${Number(p.directoriesPerSecond??0).toFixed(1)} 目录/秒（自启动均值）`),node('p',`扫描队列 ${m.directoryQueueLength??0} · 复制排队上限估计 ${m.copyQueueLength??0} · 显式属性读取 ${m.metadataCalls??0} · 原生枚举调用 ${m.nativeCalls??0} · 回退 ${m.fallbacks??0}`));}
 if(p.activeFiles.length>4)box.append(node('span',`另有 ${p.activeFiles.length-4} 个文件正在处理`,'muted'));
 box.append(node('small','每 2 秒刷新；动画仅表示运行状态，实际进展以计数和路径为准。计数随处理批次更新。写入速率包含重试，不含哈希读取；文件写完仍需校验和替换。'));
}
function renderRuns(){
 const q=el('runSearch').value.trim().toLocaleLowerCase(),status=el('runStatus').value;
 const rows=state.runs.filter(r=>(state.taskFilter===null||r.task_id===state.taskFilter)&&(!status||r.status===status)&&taskName(r).toLocaleLowerCase().includes(q));
 el('runScope').textContent=state.taskFilter===null?'范围：最近 100 次执行':'已按所选任务筛选 · 范围：最近 100 次执行';
 pager('runPager',rows.length,'runPage',renderRuns);const body=el('runs');body.replaceChildren();
 if(!rows.length){empty(body,5,'暂无符合条件的执行记录。可先从任务菜单发起一次演练。');return}
 for(const run of rows.slice((state.runPage-1)*pageSize,state.runPage*pageSize)){
  const row=body.insertRow();const title=cell(row);title.append(node('span',taskName(run),'task-name'),node('span',date(run.started),'cell-sub'));
  cell(row).append(badge(run.status));const result=parse(run.result);const summary=cell(row);
  if(run.error){summary.append(node('span',run.error,'truncate'));summary.firstChild.title=run.error}
  else if(activeRun(run))summary.textContent=run.progress?`${phaseNames[run.progress.phase]||run.progress.phase} · 已处理 ${run.progress.finished} 个文件`:'等待后台开始处理';
  else{summary.textContent=`${result.dryRun?'预计':''}复制 ${result.copied??0} · 跳过 ${result.skipped??0} · 删除 ${result.deleted??0}`;if(result.failed||result.conflicts)summary.append(node('span',`失败 ${result.failed||0} · 冲突 ${result.conflicts||0}`,'cell-sub'));if(result.ignored)summary.append(node('span',`规则忽略 ${result.ignored} 项`,'cell-sub'));if(result.unsupportedSkipped)summary.append(node('span',`已排除 ${result.unsupportedSkipped} 个链接或特殊文件`,'cell-sub'));summary.append(node('span',`${bytes(result.bytes)} · ${result.mappings?.length??1} 组目录`,'cell-sub'))}
  summary.append(node('span','比较方式：'+comparisonLabel(parse(run.snapshot)),'cell-sub'));
  cell(row,result.durationSeconds!=null?`${Number(result.durationSeconds).toFixed(2)} 秒`:'—');const ops=cell(row);button(ops,'查看报告',()=>openLogs(run),'link');button(ops,'查看日志',()=>openLogs(run),'link');if(run.status==='failed'||run.status==='partial'){const task=state.tasks.find(t=>t.id===run.task_id);if(task)button(ops,'修改任务规则',()=>{startEditor(task);form.elements.skipUnsupported.scrollIntoView({block:'center'});form.elements.skipUnsupported.focus();if((run.error||'').includes('符号链接'))message('本次失败发生在文件比较之前。若不需要备份链接，请勾选跳过后保存；需要完整保留链接时请勿跳过。',true)},'link')}
 }
}
async function refresh(manual=false){
 if(state.busy)return;state.busy=true;
 try{
  const [tasks,runs,health]=await Promise.all([api('/tasks'),api('/runs'),api('/health')]);
  state.backendReady=health.configSchemaVersion===3&&health.taskControlVersion===1&&health.networkRecoveryVersion===1&&health.scanArchitectureVersion===1&&health.comparisonDetailsVersion===1&&health.timeOptionsVersion===1&&health.progressVersion===1&&health.logQueryVersion===1;
  const changed=JSON.stringify(tasks)!==JSON.stringify(state.tasks)||JSON.stringify(runs)!==JSON.stringify(state.runs)||!state.loaded;
  state.tasks=tasks;state.runs=runs;state.loaded=true;el('connection').textContent=state.backendReady?'● 本机已连接':'○ 后台版本过旧，请重启';if(['connection','compatibility'].includes(el('message').dataset.kind))el('message').hidden=true;
  if(!state.backendReady){message(`网页与后台接口不兼容（后台 ${health.releaseVersion||'未标识版本'}，配置接口 ${health.configSchemaVersion??'未知'}）。请先刷新网页；仍不匹配时再重启单机程序。保存和运行已暂停，避免配置丢失。`,true);el('message').dataset.kind='compatibility'}
  lastProgressRefresh=Date.now();progressDisconnected=false;renderProgress(el('liveProgress'),runs.find(r=>activeRun(r)));progressFreshness();
  if(changed||manual){state.tasksNeedRender=true;state.runsNeedRender=true}
  if(state.tasksNeedRender&&!document.querySelector('.row-menu[open]')&&(!el('tasks').contains(document.activeElement)||manual)){renderTasks();state.tasksNeedRender=false}
  if(state.runsNeedRender&&(!el('runs').contains(document.activeElement)||manual)){renderRuns();state.runsNeedRender=false}
  if(state.logRun){state.logRun=runs.find(r=>r.id===state.logRun.id)||state.logRun;renderLogOverview()}
 }catch(error){progressFreshness(true);state.backendReady=false;el('connection').textContent='○ 连接中断';if(manual||!state.loaded){report(error);el('message').dataset.kind='connection';button(el('message'),'重试',()=>refresh(true));if(!state.loaded)empty(el('tasks'),5,'加载失败，请检查程序是否运行并点击重试。')}}
 finally{state.busy=false}
}
async function runTask(task,dryRun){
 if(state.runPending)return;
 if(!dryRun&&!confirm(`运行“${task.name}”？\n将同步 ${mappingsOf(task).length} 组目录。${task.direction==='bidirectional'?'双向模式会写入两侧目录，按所选冲突规则处理，不传播删除。':'变化的目标文件可能被覆盖。'}${task.delete?'\n已开启删除：目标中多余的文件将被删除。':''}`))return;
 state.runPending=true;renderTasks();
 try{await api(`/tasks/${task.id}/run`,'POST',{dryRun});state.taskFilter=task.id;state.runPage=1;el('runSearch').value='';el('runStatus').value='';await refresh(true);showTab('runs');renderRuns();message(dryRun?'演练已提交，不会修改文件。':'同步已提交，可在执行记录中查看结果。')}
 finally{state.runPending=false;renderTasks()}
}
async function deleteTask(task){if(!confirm(`删除任务“${task.name}”？\n仅删除任务配置，保留同步文件和历史日志。`))return;await api(`/tasks/${task.id}`,'DELETE');message('任务配置已删除，文件和历史日志已保留。');await refresh(true)}
function addMapping(mapping={}){
 if(el('mappings').children.length>=100){report(Error('最多支持 100 组目录'));return}
 const row=node('div',undefined,'mapping');const heading=node('div',undefined,'mapping-heading');heading.append(node('strong',`目录组 ${el('mappings').children.length+1}`));
 button(heading,'移除',()=>{if(el('mappings').children.length===1){report(Error('至少保留一组目录'));return}row.remove();state.dirty=true;[...el('mappings').children].forEach((r,i)=>r.querySelector('strong').textContent=`目录组 ${i+1}`)},'link danger');row.append(heading);
 const fields=node('div',undefined,'mapping-fields');
 for(const [key,title] of [['source','源目录'],['target','目标目录']]){const label=node('label',title);const entry=node('div',undefined,'path-entry');const input=node('input');input.dataset.path=key;input.required=true;input.value=mapping[key]||'';input.placeholder=key==='source'?'选择或输入完整目录路径':'选择或输入目标目录';entry.append(input);button(entry,'浏览',()=>openBrowser(input,title));label.append(entry);fields.append(label)}
 row.append(fields);
 const option=node('label',undefined,'check');const include=node('input');include.type='checkbox';include.dataset.includeSourceName='1';include.checked=!!mapping.includeSourceName;option.append(include,node('span','包含源目录名（在目标下创建同名目录）'));row.append(option);
 const preview=node('p',undefined,'muted');row.append(preview);
 const updatePreview=()=>{const source=row.querySelector('[data-path=source]').value.replace(/[\\/]+$/,'');const target=row.querySelector('[data-path=target]').value;const name=source.split(/[\\/]/).pop();const separator=target.includes('\\')?'\\':'/';preview.textContent='实际目标：'+(include.checked&&name?target.replace(/[\\/]+$/,'')+separator+name:target)};
 row.addEventListener('input',updatePreview);row.addEventListener('change',updatePreview);updatePreview();el('mappings').append(row);
}
function startEditor(task=null,copy=false){
 if(!leaveEditor())return;
 state.editing=copy?null:task?.id??null;state.dirty=false;for(const input of form.querySelectorAll('input,select,textarea,button'))input.disabled=false;form.reset();const values={...defaults,...task};if(task){values.comparisonMode=comparisonMode(task);values.timeToleranceSeconds=task.timeToleranceSeconds??0}
 for(const input of form.querySelectorAll('input[name],select[name],textarea[name]')){if(input.type==='checkbox')input.checked=!!values[input.name];else input.value=input.name==='ignorePatterns'?(values.ignorePatterns||[]).join('\n'):(values[input.name]??defaults[input.name])}
 if(copy)form.elements.name.value=`${task.name}（副本）`.slice(0,128);
 updateDirection();el('mappings').replaceChildren();(task?mappingsOf(task):[{}]).forEach(addMapping);
 const current=!copy&&task?state.runs.find(r=>r.task_id===task.id&&activeRun(r)):null;
 if(current){
  const editable=['name','bandwidthLimit','maxRetries','retryInterval','continueOnError','parallelism','batchFiles','largeThresholdMb','networkRetryCount','networkRetryMinutes'];
  for(const input of form.querySelectorAll('[name]'))input.disabled=current.status!=='paused'||!editable.includes(input.name);
  for(const input of el('mappings').querySelectorAll('input,button'))input.disabled=true;
  el('addMapping').disabled=current.status!=='paused';el('applyPreset').disabled=true;el('rulePreset').disabled=true;
  el('saveTask').disabled=current.status!=='paused';
 }

 if(!current)updateScanStrategy();
 el('editorTitle').textContent=copy?'复制任务配置':task?'编辑任务':'新建任务';el('editing').textContent=task&&!copy?'修改后请保存，后续运行才会使用新配置':'尚未保存';el('tab-editor').hidden=false;showTab('editor',true);state.dirty=copy;if(current)message(current.status==='paused'?'暂停编辑：已有目录已锁定，可追加目录组。限速和重试从后续文件生效，并发及批量参数从下一目录组生效。':'请先暂停并等待当前文件完成后编辑。');form.elements.name.focus();
}
form.oninput=()=>{state.dirty=true};
form.onsubmit=async event=>{
 event.preventDefault();el('saveTask').disabled=true;
 try{const data={};for(const input of form.querySelectorAll('input[name],select[name],textarea[name]'))data[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.name==='ignorePatterns'?input.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):input.value;
 data.pathMappings=[...el('mappings').children].map(row=>({source:row.querySelector('[data-path=source]').value,target:row.querySelector('[data-path=target]').value,includeSourceName:row.querySelector('[data-include-source-name]').checked}));
 if(data.syncStrategy!=='AUTO'){data.direction='one_way';data.updateOnly=false;data.delete=data.syncStrategy==='MIRROR';data.comparisonMode='size_mtime'}
 data.checksum=data.comparisonMode==='sha256';
 if(data.checksum&&!confirm('内容哈希会读取完整文件，确认启用？'))return;
 await api(state.editing?`/tasks/${state.editing}`:'/tasks',state.editing?'PUT':'POST',data);state.dirty=false;state.editing=null;el('tab-editor').hidden=true;showTab('tasks',true);message('任务已保存，可直接运行；如需预览变化，可从“更多”选择演练。');await refresh(true);
 }catch(error){report(error)}finally{el('saveTask').disabled=false}
};
function cancelEditor(){if(showTab('tasks')){el('tab-editor').hidden=true;state.editing=null}}
el('cancelEdit').onclick=cancelEditor;el('discardEdit').onclick=cancelEditor;el('newTask').onclick=()=>startEditor();el('addMapping').onclick=()=>{addMapping();state.dirty=true};
window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue=''}});
el('taskSearch').oninput=()=>{state.taskPage=1;renderTasks()};
for(const id of ['runSearch','runStatus'])el(id).addEventListener(id==='runSearch'?'input':'change',()=>{state.runPage=1;renderRuns()});
el('clearRunFilter').onclick=()=>{state.taskFilter=null;state.runPage=1;el('runSearch').value='';el('runStatus').value='';renderRuns()};
el('refreshTasks').onclick=()=>refresh(true);el('refreshRuns').onclick=()=>refresh(true);el('chooseRun').onclick=()=>showTab('runs');
function renderLogOverview(){if(!state.logRun)return;const run=state.logRun;el('logSubtitle').textContent=`${taskName(run)} · ${date(run.started)}`;const box=el('logOverview');box.replaceChildren(node('strong',taskName(run)),badge(run.status),node('span',`执行 #${run.id} · ${date(run.started)}`,'muted'));box.append(node('span','比较方式：'+comparisonLabel(parse(run.snapshot)),'muted'));if(run.error)box.append(node('span',run.error,'run-error'));if(parse(run.snapshot).dryRun)box.append(node('span','演练日志：复制 / 删除均为预计动作','muted'))}
let logSearchTimer,reportRequest=0;
function filterLogs(action){
 clearTimeout(logSearchTimer);el('logAction').value=action;el('logSearch').value='';el('logMapping').value='';state.logPage=1;loadLogs();
}
async function loadReport(runId){
 const request=++reportRequest;
 let report;
 try{report=await api(`/runs/${runId}/report`)}catch(error){if(request===reportRequest&&state.logRun?.id===runId)el('runReport').replaceChildren(node('p','报告加载失败，请点击“刷新结果”重试。'));throw error}
 if(state.logRun?.id!==runId||request!==reportRequest)return;
 const box=el('runReport');box.replaceChildren(node('h2','同步结果报告'));
 const dry=parse(report.run.snapshot).dryRun,counts=report.counts;
 box.append(node('p',`${dry?'演练预计复制量':'已复制数据量'}：${bytes(report.copiedBytes)} · 已记录 ${report.totalEvents.toLocaleString()} 条日志`));
 const metrics=node('div',undefined,'report-metrics');
 for(const [key,label] of [['copied',dry?'预计复制':'复制成功'],['skipped','跳过'],['ignored','规则忽略'],['failed','文件失败'],['conflict','冲突'],['deleted',dry?'预计删除':'删除'],['mapping_failed','目录组失败']]){
  button(metrics,`${label} ${(counts[key]||0).toLocaleString()}`,()=>filterLogs(key),'report-metric');
 }
 box.append(metrics,node('small',report.partial?'任务运行中：以上为已落库记录，刷新结果可更新；未提交批次暂不计入。':'点击任一数量查看对应文件。跳过包括被排除的链接，规则忽略单独计数。'));
 if(['failed','interrupted'].includes(report.run.status))box.append(node('p','本次未正常完成：以上仅为已记录数量，可能不完整；请结合错误日志核查目标文件。','run-error'));
 box.append(node('small','报告更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false})));
 const savedResult=parse(report.run.result);
 for(const [index,mapping] of (savedResult.mappings||[]).entries()){const m=mapping.scanMetrics;if(m)box.append(node('p',`目录组 ${index+1}：源枚举 ${m.sourceEntries} / 目标枚举 ${m.targetEntries} 项；显式属性 API ${m.metadataCalls}，原生枚举 API ${m.nativeCalls}，回退 ${m.fallbacks} 次。`));}
 const duration=savedResult.durationSeconds;
 if(duration!=null)box.append(node('p',`同步耗时：${Number(duration).toFixed(2)} 秒`));
}
async function openLogs(run){
 clearTimeout(logSearchTimer);state.logRun=run;state.logPage=1;state.logRows=[];empty(el('logs'),4,'正在加载日志…');
 el('runReport').replaceChildren(node('p','正在汇总报告…'));
 el('logSearch').value='';el('logAction').value='';el('logMapping').replaceChildren(new Option('全部目录组',''));
 mappingsOf(parse(run.snapshot)).forEach((m,i)=>el('logMapping').add(new Option(`目录组 ${i+1} · ${m.source||''}`,String(i+1))));
 el('logEmpty').hidden=true;el('logContent').hidden=false;showTab('logs');renderLogOverview();
 await Promise.all([loadLogs(),loadReport(run.id).catch(report)]);
}
async function loadLogs(){
 if(!state.logRun)return;const request=++state.logRequest,runId=state.logRun.id;
 for(const id of ['logFirst','logPrev','logNext','logLast','logGo'])el(id).disabled=true;
 const params=new URLSearchParams({page:state.logPage,page_size:el('logPageSize').value,action:el('logAction').value,q:el('logSearch').value,order:el('logOrder').value});
 if(el('logMapping').value)params.set('mapping',el('logMapping').value);
 try{const data=await api(`/runs/${runId}/logs?${params}`);if(request!==state.logRequest)return;state.logRows=data.items;state.logPage=data.page;state.logTotal=data.total;state.logPages=data.pages;renderLogs()}
 catch(error){if(request===state.logRequest){state.logRows=[];empty(el('logs'),4,'日志加载失败，点击“刷新结果”重试。');el('logCount').textContent='加载失败';el('logPageText').textContent='—';report(error)}}
}
function renderLogs(){
 const rows=state.logRows.map(r=>({...r,event:parse(r.detail)}));
 el('logCount').textContent=`共 ${state.logTotal.toLocaleString()} 条匹配日志`;
 el('logPageText').textContent=`第 ${state.logPage} / ${state.logPages} 页 · 本页 ${rows.length} 条`;
 el('logPrev').disabled=el('logFirst').disabled=state.logPage<=1;el('logNext').disabled=el('logLast').disabled=state.logPage>=state.logPages;el('logGo').disabled=false;el('logJump').value=state.logPage;el('logJump').max=state.logPages;
 const body=el('logs');body.replaceChildren();if(!rows.length){empty(body,4,'没有符合条件的日志，可清除筛选或刷新结果。');return}
 for(const row of rows){const event=row.event,tr=body.insertRow();cell(tr,date(row.created));cell(tr).append(node('span',event.action==='copied'?(parse(state.logRun.snapshot).dryRun?'预计复制':'复制成功'):event.action==='deleted'&&parse(state.logRun.snapshot).dryRun?'预计删除':actions[event.action]||event.action||'事件','badge '+(String(event.action).includes('failed')?'failed':event.action==='conflict'?'partial':'')));
 const path=cell(tr);path.append(node('span',`目录组 ${event.mappingIndex??'—'}${event.direction==='source_to_target'?' · 源 → 目标':event.direction==='target_to_source'?' · 目标 → 源':event.direction==='both'?' · 两端比较':''}`,'cell-sub'),node('span',event.path||`${event.source||''} → ${event.target||''}`));
 const detail=cell(tr);detail.append(node('span',event.error||event.reason|| (event.action==='mapping_completed'?'本组处理完成':event.bytes!=null?`${bytes(event.bytes)}${event.retries?` · 重试 ${event.retries} 次`:''}`:'—')));
 if(event.retryCount!=null)detail.append(node('span',`网络重试 ${event.retryCount}/${event.retryLimit} · ${event.waitSeconds==null?'次数已用尽，等待管理员恢复':event.waitSeconds+' 秒后重试'}`,'cell-sub'));
 if(event.comparison){const info=node('details',undefined,'log-detail comparison-detail');info.append(node('summary','查看大小 / 时间 / 哈希比较'));const lines=['比较方式：'+comparisonLabel({comparisonMode:event.comparison.method}), '判断：'+event.comparison.reason];for(const [key,label] of [['source','源文件'],['target','执行前目标文件']]){const item=event.comparison[key];lines.push(item?`${label}：${item.size} 字节；修改时间 ${date(Number(item.mtimeNs)/1e6)}；纳秒时间戳 ${item.mtimeNs}；SHA-256：${item.sha256||'未计算'}`:`${label}：不存在`)}lines.push('时间容差：'+(event.comparison.timeToleranceSeconds??0)+' 秒');if(event.timestamps)lines.push('复制后修改时间：'+date(Number(event.timestamps.targetMtimeNs)/1e6)+'；'+(event.timestamps.preserved?'保留源修改时间':'使用写入时间')+'；与源相差 '+Number(event.timestamps.differenceNs)/1e9+' 秒');if(event.verification)lines.push('写入后 SHA-256：'+event.verification.writtenSha256);info.append(node('pre',lines.join('\n')));detail.append(info)}
 const raw=node('details',undefined,'log-detail');raw.append(node('summary','原始详情'),node('pre',JSON.stringify(event,null,2)));detail.append(raw);
 }
}
el('logSearch').oninput=()=>{clearTimeout(logSearchTimer);++state.logRequest;logSearchTimer=setTimeout(()=>{state.logPage=1;loadLogs()},300)};
for(const id of ['logAction','logMapping','logOrder','logPageSize'])el(id).onchange=()=>{clearTimeout(logSearchTimer);state.logPage=1;loadLogs()};
el('clearLogs').onclick=()=>filterLogs('');
el('refreshLogs').onclick=()=>{clearTimeout(logSearchTimer);loadLogs();if(state.logRun)loadReport(state.logRun.id).catch(report)};
el('logPrev').onclick=()=>{if(state.logPage>1){state.logPage--;loadLogs()}};
el('logNext').onclick=()=>{if(state.logPage<state.logPages){state.logPage++;loadLogs()}};
el('logFirst').onclick=()=>{state.logPage=1;loadLogs()};
el('logLast').onclick=()=>{state.logPage=state.logPages;loadLogs()};
el('logGo').onclick=()=>{const page=Number(el('logJump').value);if(!Number.isInteger(page)||page<1||page>state.logPages){report(Error('请输入有效页码'));return}state.logPage=page;loadLogs()};
el('logJump').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();el('logGo').click()}};
document.addEventListener('keydown',event=>{if(event.key==='Escape')for(const menu of document.querySelectorAll('.row-menu[open]')){menu.open=false;menu.querySelector('summary').focus()}});
document.addEventListener('click',event=>{for(const menu of document.querySelectorAll('.row-menu[open]'))if(!menu.contains(event.target))menu.open=false});
// 目录筛选由后台完成，确保搜索和排序覆盖分页外的目录。
let selectedInput=null,browseState=null,browseRequest=0,browsePath='',browseTimer=null,browseLoaded=0;
function folderControls(disabled){for(const id of ['folderSearch','folderSort','folderClear'])el(id).disabled=disabled}
function invalidateBrowse(){clearTimeout(browseTimer);browseRequest++;el('folderSelect').disabled=true;el('folderMore').hidden=true;el('folderUp').disabled=true}
async function browse(path='',offset=0){
 clearTimeout(browseTimer);const request=++browseRequest;browsePath=path;
 el('folderSelect').disabled=true;el('folderMore').hidden=true;el('folderUp').disabled=true;el('folderError').textContent='';el('currentFolder').textContent='正在读取目录…';
 if(offset===0){browseLoaded=0;el('folderList').replaceChildren();el('folderCount').textContent=''}
 try{
  const data=await api('/directories?'+new URLSearchParams({path,offset,query:el('folderSearch').value,sort:el('folderSort').value}));
  if(request!==browseRequest)return;
  browseState=data;browsePath=data.path;el('folderAddress').value=data.path;el('currentFolder').textContent=data.path||'请选择磁盘或常用位置';folderControls(false);
  for(const directory of data.directories){
   const b=button(el('folderList'),directory.name,()=>navigateFolder(directory.path));b.title=directory.path;
   if(directory.modified!=null)b.append(node('small',date(directory.modified*1000),'folder-time'));
  }
  browseLoaded+=data.directories.length;
  el('folderCount').textContent=`${el('folderSearch').value.trim()?'匹配':'共'} ${data.total} 个目录 · 已显示 ${browseLoaded} 个`;
  if(!offset&&!data.directories.length)el('folderList').textContent=el('folderSearch').value.trim()?'没有匹配的子目录，可修改关键词或清空搜索。':'没有可浏览的子目录';
  el('folderSelect').disabled=!data.path;el('folderUp').disabled=!data.parent;el('folderMore').hidden=data.nextOffset===null;
 }catch(error){if(request!==browseRequest)return;browseState=null;el('folderList').replaceChildren();el('currentFolder').textContent='';el('folderCount').textContent='';el('folderError').textContent=error.message;el('folderUp').disabled=true}
}
function navigateFolder(path=''){el('folderSearch').value='';folderControls(false);el('folderAddress').value=path;return browse(path)}
function openBrowser(input,title){selectedInput=input;el('folderTitle').textContent='选择'+title;el('folderDialog').showModal();navigateFolder(input.value)}
function closeBrowser(){invalidateBrowse();el('folderDialog').close();selectedInput=null}
el('folderGo').onclick=()=>navigateFolder(el('folderAddress').value);
// 改地址时不再接受之前的目录请求，搜索必须等新地址打开后再执行。
el('folderAddress').oninput=()=>{invalidateBrowse();browseState=null;folderControls(true);el('folderList').replaceChildren();el('folderCount').textContent='';el('currentFolder').textContent='地址已修改，点击“打开”浏览该目录'};
el('folderAddress').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();navigateFolder(el('folderAddress').value)}};
el('folderSearch').oninput=()=>{invalidateBrowse();el('folderList').replaceChildren();el('folderCount').textContent='正在搜索…';browseTimer=setTimeout(()=>browse(browsePath),250)};
el('folderSearch').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();browse(browsePath)}};
el('folderSort').onchange=()=>browse(browsePath);
el('folderClear').onclick=()=>{el('folderSearch').value='';browse(browsePath)};
el('folderRoots').onclick=()=>navigateFolder();
el('folderUp').onclick=()=>{if(browseState?.parent)navigateFolder(browseState.parent)};
el('folderMore').onclick=()=>{if(browseState?.nextOffset!=null)browse(browseState.path,browseState.nextOffset)};
el('folderCancel').onclick=closeBrowser;
el('folderDialog').oncancel=event=>{event.preventDefault();closeBrowser()};
el('folderSelect').onclick=()=>{if(selectedInput&&browseState?.path){selectedInput.value=browseState.path;selectedInput.dispatchEvent(new Event('input',{bubbles:true}));state.dirty=true;closeBrowser()}};
refresh();setInterval(()=>{if(!document.hidden){progressFreshness();refresh()}},2000);

// 预设仅填表，不立即保存或执行；旧任务缺少新字段时使用兼容默认值。
const presets={
 incremental:{syncStrategy:'AUTO',direction:'one_way',delete:false,updateOnly:false,comparisonMode:'size',preserveTime:true,timeToleranceSeconds:2,continueOnError:false,skipUnsupported:true,ignorePatterns:[],conflictPolicy:'skip'},
 mirror:{syncStrategy:'AUTO',direction:'one_way',delete:true,updateOnly:false,comparisonMode:'size',preserveTime:true,timeToleranceSeconds:2,continueOnError:false,skipUnsupported:true,ignorePatterns:[],conflictPolicy:'skip'},
 merge:{syncStrategy:'AUTO',direction:'bidirectional',delete:false,updateOnly:false,comparisonMode:'size',preserveTime:true,timeToleranceSeconds:2,continueOnError:false,skipUnsupported:true,ignorePatterns:[],conflictPolicy:'skip'},
 project:{syncStrategy:'AUTO',direction:'one_way',delete:false,updateOnly:false,comparisonMode:'size',preserveTime:true,timeToleranceSeconds:2,continueOnError:true,skipUnsupported:true,ignorePatterns:['.git/','node_modules/','.venv/','__pycache__/','*.pyc','.DS_Store','Thumbs.db'],conflictPolicy:'skip'}
};
function updateDirection(){
 const twoWay=form.elements.direction.value==='bidirectional';
 el('conflictField').hidden=!twoWay;
 for(const name of ['delete','updateOnly']){form.elements[name].disabled=twoWay;if(twoWay)form.elements[name].checked=false}
 el('modeHint').textContent=twoWay?'双向合并会写入两侧目录；不传播删除，单侧删除的文件可能被另一侧补回。按所选方式比较；仅大小模式会跳过同大小异内容文件，不触发冲突处理。可选时间或哈希比较。':'单向同步源目录到目标目录；默认保留目标多余文件。可直接运行，演练完全可选。';
}
el('syncDirection').onchange=()=>{updateDirection();state.dirty=true};
el('applyPreset').onclick=()=>{
 const preset=presets[el('rulePreset').value];
 for(const [key,value] of Object.entries(preset)){const field=form.elements[key];if(field.type==='checkbox')field.checked=value;else field.value=Array.isArray(value)?value.join('\n'):value}
 updateScanStrategy();state.dirty=true;message('规则预设已填入，请检查后保存任务。');
};

// 保留现有任务编辑页；新策略的固定语义直接展示，避免保存时暗改选项。
function updateScanStrategy(){
 const strategy=form.elements.syncStrategy.value, explicit=strategy!=='AUTO';
 if(explicit){form.elements.direction.value='one_way';form.elements.updateOnly.checked=false;form.elements.delete.checked=strategy==='MIRROR';form.elements.comparisonMode.value='size_mtime'}
 updateDirection();
 for(const name of ['direction','comparisonMode'])form.elements[name].disabled=explicit;
 for(const name of ['updateOnly','delete'])form.elements[name].disabled=explicit||form.elements.direction.value==='bidirectional';
 if(explicit)el('modeHint').textContent={COPY_ALL:'全部覆盖：仅扫描源；现有同名文件也会重新写入。',SKIP_EXISTING:'已有名称即跳过，不读取大小、时间或内容。',COMPARE_METADATA:'按目录比较大小和时间；相同则跳过，时间容差仍生效。',MIRROR:'镜像将删除目标多余内容；仅在复制和扫描无错误后执行删除。'}[strategy];
}
el('syncStrategy').addEventListener('change',updateScanStrategy);
