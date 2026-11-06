'use strict';
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem('home-music-token') || '';
let pollTimer;
let libraryDisplayCount = 100;
let refreshGeneration = 0;
const states = {queued:'等待保存',downloading:'正在保存',ready:'已保存',failed:'保存失败',missing:'文件不在原位置'};
function message(text){ $('message').textContent = text; }
function element(tag, text, cls){ const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; }
async function api(path, data){
  const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(15000)});
  const payload=await response.json();
  if(!response.ok || payload.code!==0)throw new Error(payload.msg||'请求失败');
  return payload.data;
}
function link(text,url){const node=element('a',text);node.href=url;node.target='_blank';node.rel='noopener noreferrer';return node;}
function card(track, saved){
  const node=element('article',undefined,'card');
  node.append(element('h3',track.title),element('p',track.artist||'作者信息见来源页面'));
  const duration=Math.round(track.duration||0);node.append(element('p',`${Math.floor(duration/60)}:${String(duration%60).padStart(2,'0')} · ${track.license_name}`));
  if(saved){node.append(element('span',states[track.status]||track.status,'state '+track.status));if(track.error)node.append(element('p',track.error));if(track.aliases?.length)node.append(element('p','点歌名称：'+track.aliases.join('、')));}
  if(saved && track.status==='ready'){
    const sync=track.songloft_sync||{state:'disabled'};
    const syncLabels={synced:'已同步到主曲库和歌单',pending:'主曲库同步待完成，将自动重试',disabled:'主曲库自动同步未配置'};
    node.append(element('p',sync.error||syncLabels[sync.state]||'主曲库同步待确认','small'));
  }
  const actions=element('div',undefined,'actions');
  if(!saved || ['failed','missing'].includes(track.status)){
    const add=element('button',saved?'重试保存':'保存到家里');add.addEventListener('click',async()=>{
      const alias=window.prompt('设置中文点歌名称（可留空，沿用原标题）',saved?(track.aliases?.[0]||''):$('keyword').value);
      if(alias===null)return;
      add.disabled=true;
      try{await api('/api/import',{page_id:track.page_id,alias:alias.trim()});message('已加入保存队列，请等待“已保存”状态。');await refresh();}
      catch(e){message(e.message);}finally{add.disabled=false;}
    });actions.append(add);
  }
  if(saved){
    const aliasButton=element('button','设置点歌名','quiet');aliasButton.addEventListener('click',async()=>{
      const alias=window.prompt('例如：我的睡前音乐、致爱丽丝',track.aliases?.[0]||track.title);if(!alias)return;
      try{await api('/api/alias',{id:track.id,alias});message('点歌名已保存。可以说：小爱同学，播放歌曲'+alias);await refresh();}catch(e){message(e.message);}
    });actions.append(aliasButton);
    if(track.status==='ready'){
      const play=element('button','试听','quiet');play.addEventListener('click',()=>{
        let audio=node.querySelector('audio');if(!audio){audio=element('audio');audio.controls=true;audio.preload='none';audio.src=track.play_url;node.append(audio);}audio.play().catch(()=>message('请点击音频播放器中的播放按钮。'));
      });actions.append(play);
      const download=element('a','下载 MP3');download.href=track.play_url;download.download=track.title+'.mp3';actions.append(download);
    }
  }
  actions.append(link('来源与版本',track.source_url),link('许可说明',track.license_url));node.append(actions);return node;
}
async function refresh(automatic=false){
  if(automatic && [...$('library').querySelectorAll('audio')].some(audio=>!audio.paused)){
    pollTimer=setTimeout(()=>refresh(true).catch(e=>message(e.message)),4000);return;
  }
  const generation=++refreshGeneration;
  const tracks=[];
  let more=false;
  for(let offset=0;offset<libraryDisplayCount;offset+=100){
    const page=await api('/api/library?limit=100&offset='+offset);
    if(generation!==refreshGeneration)return;
    tracks.push(...page);more=page.length===100;
    if(!more)break;
  }
  $('load-more').hidden=!more;$('library').replaceChildren();
  if(!tracks.length)$('library').append(element('p','还没有保存音乐。可以先搜索“致爱丽丝”试试。'));
  tracks.forEach(track=>$('library').append(card(track,true)));
  clearTimeout(pollTimer);
  if(tracks.some(track=>['queued','downloading'].includes(track.status)||(track.status==='ready' && track.songloft_sync?.state==='pending')))pollTimer=setTimeout(()=>refresh(true).catch(e=>message(e.message)),4000);
}
async function login(){await refresh();sessionStorage.setItem('home-music-token',token);$('login-panel').hidden=true;$('workspace').hidden=false;message('');}
$('login-form').addEventListener('submit',async(e)=>{
  e.preventDefault();token=$('token').value.trim();
  const feedback=$('login-feedback'),button=$('connect-button');
  if(!token){feedback.textContent='请先双击部署目录里的 open-music.bat，然后在密钥框按 Ctrl+V 粘贴。';$('token').focus();return;}
  button.disabled=true;button.textContent='正在连接…';feedback.textContent='正在验证本地访问密钥…';
  try{await login();$('token').value='';feedback.textContent='';}
  catch(error){feedback.textContent=error.name==='TimeoutError'?'连接超时，请确认本地服务已经启动，再重试。':error.message;}
  finally{button.disabled=false;button.textContent='连接';}
});
$('logout').addEventListener('click',()=>{token='';++refreshGeneration;sessionStorage.removeItem('home-music-token');clearTimeout(pollTimer);$('workspace').hidden=true;$('login-panel').hidden=false;message('已退出');});
$('refresh').addEventListener('click',()=>refresh().catch(e=>message(e.message)));
$('load-more').addEventListener('click',async()=>{
  $('load-more').disabled=true;libraryDisplayCount+=100;
  try{await refresh();}catch(e){message(e.message);}finally{$('load-more').disabled=false;}
});
$('search-form').addEventListener('submit',async(e)=>{
  e.preventDefault();$('search-button').disabled=true;message('正在检索开放授权录音…');$('results').replaceChildren();
  try{const results=await api('/api/search',{keyword:$('keyword').value.trim()});results.forEach(track=>$('results').append(card(track,false)));message(results.length?`找到 ${results.length} 个可保存版本，请核对演奏者与来源。`:'没有找到符合条件的录音。可尝试英文曲名，或把已有 MP3 放入手动曲库。');}
  catch(error){message(error.message);}finally{$('search-button').disabled=false;}
});
if(token)login().catch(()=>{sessionStorage.removeItem('home-music-token');token='';message('请重新填写访问密钥。');});
