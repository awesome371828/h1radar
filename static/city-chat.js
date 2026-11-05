(function(){
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path,opts={}){const token=localStorage.getItem("h1_session")||"";const r=await fetch("/api"+path,{...opts,headers:{"Content-Type":"application/json",...(token?{Authorization:"Bearer "+token}:{}),...(opts.headers||{})}});const d=await r.json();if(!r.ok)throw Error(d.detail||"Ошибка запроса");return d}
async function load(){const city=$("citySelect")?.value;if(!city){$("cityChat").innerHTML='<div class="empty">Сначала выбери город.</div>';return}try{const d=await api("/city-chat?city="+encodeURIComponent(city));$("cityChat").innerHTML=d.items.map(x=>'<article class="chatmsg"><b>'+esc(x.name)+'</b> <small>'+(x.username?"@"+esc(x.username):"")+'</small><p>'+esc(x.message)+'</p></article>').join("")||'<div class="empty">Пока сообщений нет. Начни общение.</div>'}catch(e){$("cityChat").textContent=e.message}}
function open(){["feed","about","chat","moderation"].forEach(x=>{const p=$(x+"Panel");if(p)p.hidden=x!=="chat"});load()}
document.querySelectorAll('[data-tab="chat"]').forEach(b=>b.addEventListener("click",open));
document.querySelectorAll('[data-mobile="chat"]').forEach(b=>b.addEventListener("click",()=>{document.querySelector('[data-tab="chat"]').click()}));
$("citySelect")?.addEventListener("change",()=>{if(!$("chatPanel").hidden)load()});
$("sendChat")?.addEventListener("click",async()=>{const msg=$("chatMessage").value.trim();if(!msg)return;try{await api("/city-chat",{method:"POST",body:JSON.stringify({city:$("citySelect").value,message:msg})});$("chatMessage").value="";load()}catch(e){if(window.toast)toast(e.message);else alert(e.message)}});
setInterval(()=>{if($("chatPanel")&&!$("chatPanel").hidden)load()},8000);
})();