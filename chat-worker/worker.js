const MODEL = "svjack/gpt-oss-20b-heretic";

const PAGE = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zeid Data — Chat</title>
<style>
  :root { --bg:#0d0f12; --panel:#15181d; --line:#242a31; --txt:#e6e9ee; --dim:#8a929c; --accent:#c0392b; --user:#1c2530; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; }
  body { display:flex; flex-direction:column; background:var(--bg); color:var(--txt); font-family:-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line); font-weight:600; display:flex; justify-content:space-between; align-items:center; }
  header .sub { color:var(--dim); font-weight:400; font-size:13px; }
  #log { flex:1; overflow-y:auto; padding:20px; display:flex; flex-direction:column; gap:12px; }
  .msg { max-width:760px; width:fit-content; padding:12px 15px; border-radius:10px; line-height:1.5; white-space:pre-wrap; word-wrap:break-word; }
  .user { align-self:flex-end; background:var(--user); }
  .bot { align-self:flex-start; background:var(--panel); border:1px solid var(--line); }
  .think { align-self:flex-start; max-width:760px; font-size:13px; color:var(--dim); border-left:2px solid var(--line); padding:4px 12px; white-space:pre-wrap; display:none; }
  .think.show { display:block; }
  form { display:flex; gap:10px; padding:14px 18px; border-top:1px solid var(--line); }
  textarea { flex:1; resize:none; background:var(--panel); color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:11px 13px; font:inherit; font-size:15px; height:46px; max-height:160px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:0 20px; font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #gate { position:fixed; inset:0; background:var(--bg); display:flex; align-items:center; justify-content:center; flex-direction:column; gap:14px; z-index:10; }
  #gate input { background:var(--panel); color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:11px 13px; font-size:15px; width:260px; }
  #gate .t { font-weight:600; }
  #gate .e { color:var(--accent); font-size:13px; height:16px; }
</style>
</head>
<body>
<div id="gate">
  <div class="t">Zeid Data — Access</div>
  <input id="pw" type="password" placeholder="Access password" autocomplete="off">
  <button id="enter">Enter</button>
  <div class="e" id="gateErr"></div>
</div>
<header><span>Zeid Data</span><span class="sub" id="status">ready</span></header>
<div id="log"></div>
<form id="form">
  <textarea id="input" placeholder="Message..." autocomplete="off"></textarea>
  <button id="send" type="submit">Send</button>
</form>
<script>
var history = [];
var busy = false;
function pw(){ return sessionStorage.getItem("zd_pw") || ""; }
var gate = document.getElementById("gate");
document.getElementById("enter").onclick = function(){
  var v = document.getElementById("pw").value;
  if(!v){ return; }
  sessionStorage.setItem("zd_pw", v);
  gate.style.display="none";
};
document.getElementById("pw").addEventListener("keydown", function(e){ if(e.key==="Enter"){ document.getElementById("enter").click(); }});
if(pw()){ gate.style.display="none"; }
var log = document.getElementById("log");
var input = document.getElementById("input");
var sendBtn = document.getElementById("send");
var statusEl = document.getElementById("status");
function addMsg(cls, text){
  var d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}
function addThink(){
  var d = document.createElement("div");
  d.className = "think";
  log.appendChild(d);
  return d;
}
document.getElementById("form").addEventListener("submit", function(e){ e.preventDefault(); send(); });
input.addEventListener("keydown", function(e){ if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); send(); }});
function send(){
  if(busy){ return; }
  var text = input.value.trim();
  if(!text){ return; }
  input.value="";
  history.push({role:"user", content:text});
  addMsg("user", text);
  busy = true; sendBtn.disabled = true; statusEl.textContent = "thinking...";
  var thinkEl = addThink();
  var botEl = addMsg("bot", "");
  var botText = ""; var thinkText = "";
  function done(){ busy=false; sendBtn.disabled=false; statusEl.textContent="ready"; input.focus(); }
  fetch("/api/chat", {
    method:"POST",
    headers:{ "Content-Type":"application/json", "x-chat-password": pw() },
    body: JSON.stringify({ messages: history, stream:true })
  }).then(function(resp){
    if(resp.status===401){ sessionStorage.removeItem("zd_pw"); gate.style.display="flex"; document.getElementById("gateErr").textContent="Wrong password"; throw new Error("unauthorized"); }
    if(!resp.ok || !resp.body){ throw new Error("http "+resp.status); }
    var reader = resp.body.getReader();
    var dec = new TextDecoder();
    var buf = "";
    function pump(){
      return reader.read().then(function(r){
        if(r.done){ history.push({role:"assistant", content:botText}); done(); return; }
        buf += dec.decode(r.value, {stream:true});
        var lines = buf.split("\n");
        buf = lines.pop();
        for(var i=0;i<lines.length;i++){
          var line = lines[i].trim();
          if(!line){ continue; }
          try{
            var obj = JSON.parse(line);
            if(obj.message){
              if(obj.message.thinking){ thinkText += obj.message.thinking; thinkEl.textContent = thinkText; thinkEl.classList.add("show"); }
              if(obj.message.content){ botText += obj.message.content; botEl.textContent = botText; }
            }
          }catch(err){}
        }
        log.scrollTop = log.scrollHeight;
        return pump();
      });
    }
    return pump();
  }).catch(function(err){
    if(err.message!=="unauthorized"){ botEl.textContent = botText || ("[error: "+err.message+"]"); }
    done();
  });
}
</script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      return new Response(PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
    if (request.method === "POST" && url.pathname === "/api/chat") {
      const given = request.headers.get("x-chat-password") || "";
      if (given !== env.CHAT_PASSWORD) {
        return new Response("Unauthorized", { status: 401 });
      }
      let payload;
      try { payload = await request.json(); } catch (e) { return new Response("Bad JSON", { status: 400 }); }
      payload.model = MODEL;
      const upstream = await fetch(env.BOX_URL + "/api/chat", {
        method: "POST",
        headers: { "Authorization": "Bearer " + env.BOX_TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return new Response(upstream.body, {
        status: upstream.status,
        headers: { "Content-Type": "application/x-ndjson" },
      });
    }
    return new Response("Not found", { status: 404 });
  }
};
