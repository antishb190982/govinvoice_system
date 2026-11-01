/** Read-only capture of current demo screens. Session and redactions stay in memory. */
import fs from "node:fs/promises";
import {spawn, execFileSync} from "node:child_process";
import path from "node:path";
import {fileURLToPath} from "node:url";

const app = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const server = await fs.access(path.join(app,"server","manage.py")).then(
  () => path.join(app,"server"), () => app);
const root = server === app ? app : path.resolve(app,"../..");
const output = path.join(root, "generated-artifacts/training/screens");
await fs.mkdir(output, {recursive:true});
const env = {...process.env, GOVBILLS_ENV:"development", DJANGO_SETTINGS_MODULE:"config.replit_preview"};
const setup = `
import django, json, time
django.setup()
from django.test import Client
from accounts.models import User
from accounts.middleware import OTP_SESSION_KEY
from providers.models import ServiceAccount
u=User.objects.get(username="sysadmin", is_active=True, is_superuser=True)
c=Client(); c.force_login(u)
s=c.session; s[OTP_SESSION_KEY]=True; s["last_activity"]=int(time.time()); s.save()
redactions=[]
for p in User.objects.all():
    if p.full_name and len(p.full_name)>4: redactions.append(p.full_name)
    if p.email: redactions.append(p.email)
redactions += [str(a) for a in ServiceAccount.objects.values_list("account_number", flat=True) if len(str(a))>4]
print(json.dumps({"session":s.session_key,"redactions":redactions}))
`;
const auth = JSON.parse(execFileSync("python", ["-c",setup], {cwd:server,env,encoding:"utf8"}));
const browser = spawn("chromium", ["--headless","--no-sandbox","--disable-dev-shm-usage",
  "--remote-debugging-address=127.0.0.1","--remote-debugging-port=9333",
  "--user-data-dir=/tmp/govbills-training-browser","about:blank"], {stdio:"ignore"});
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
let ws;
try {
  let targets;
  for(let i=0;i<80;i++){
    try { targets=await (await fetch("http://127.0.0.1:9333/json")).json(); if(targets.length)break; } catch {}
    await sleep(250);
  }
  if(!targets?.length)throw new Error("Capture browser did not start");
  ws = new WebSocket(targets.find(t=>t.type==="page").webSocketDebuggerUrl);
  await new Promise(resolve=>ws.addEventListener("open",resolve,{once:true}));
  let seq=0;const pending=new Map();
  ws.addEventListener("message",event=>{
    const result=JSON.parse(event.data);
    if(!result.id)return;
    const handler=pending.get(result.id);pending.delete(result.id);
    result.error?handler.reject(new Error(result.error.message)):handler.resolve(result.result);
  });
  const call=(method,params={})=>new Promise((resolve,reject)=>{
    const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));
  });
  const evaluate=async expression=>{
    const result=await call("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
    if(result.exceptionDetails)throw new Error("Capture evaluation failed");
    return result.result.value;
  };
  await call("Page.enable"); await call("Network.enable");
  await call("Network.setCookie",{name:"sessionid",value:auth.session,url:"http://127.0.0.1:24040/",
    httpOnly:true,secure:false,sameSite:"Lax"});
  await call("Emulation.setDeviceMetricsOverride",{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  const captures={
    "my-work":"/my-work/","intake":"/imports/","preparation":"/operations/prepare/",
    "batch":"/operations/batches/10/","approvals":"/approvals/","workflow":"/approvals/policies/",
    "reconciliation":"/reconciliation/","settlements":"/settlements/",
    "bank":"/reconciliation/bank/","accounts":"/providers/accounts/",
    "timeliness":"/reports/ministry-timeliness/","overview":"/dashboard/",
    "audit":"/audit/","workspace":"/operations/my-batches/?folder=all",
    "admin":"/console/","users":"/accounts/users/","gateways":"/console/gateways/"
  };
  const manifest=[];
  for(const [name,route] of Object.entries(captures)){
    await call("Page.navigate",{url:"http://127.0.0.1:24040"+route});
    await sleep(900);
    await evaluate("document.fonts.ready.then(()=>true)");
    const state=await evaluate("({path:location.pathname,title:document.title,text:document.body.innerText.slice(0,250)})");
    if(state.path.includes("/login") || /Not Found|Forbidden|Server Error/.test(state.title+state.text))
      throw new Error(`Cannot capture ${name}: ${state.title}`);
    await evaluate(`(()=>{
      const redact=${JSON.stringify(auth.redactions)};
      const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
      for(const node of nodes){
        if(["SCRIPT","STYLE"].includes(node.parentElement?.tagName))continue;
        let text=node.textContent;
        for(const value of redact)text=text.split(value).join(value.includes("@")?"demo@example.invalid":"Demo record");
        text=text.replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/gi,"demo@example.invalid");
        node.textContent=text;
      }
      document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio])").forEach(el=>{
        if(el.type!=="submit"&&el.type!=="button")el.value="";
      });
      document.querySelectorAll("[data-notification-count],.side-count").forEach(el=>el.textContent="");
      return true;
    })()`);
    const image=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
    await fs.writeFile(path.join(output,name+".png"),Buffer.from(image.data,"base64"));
    manifest.push({name,route,captured_at:new Date().toISOString(),title:state.title,
      image:`screens/${name}.png`,capture_role:"demo system administrator",privacy:"Names, email addresses and allocated account numbers masked; no source invoice documents captured."});
    console.log("Captured",name,state.title);
  }
  await fs.writeFile(path.join(output,"manifest.json"),JSON.stringify(manifest,null,2));
} finally {
  if(ws)ws.close();
  browser.kill("SIGTERM");
  execFileSync("python",["-c",`import django,json,sys;django.setup();from django.contrib.sessions.models import Session;Session.objects.filter(session_key=json.load(sys.stdin)["session"]).delete()`],
    {cwd:server,env,input:JSON.stringify({session:auth.session}),stdio:["pipe","ignore","ignore"]});
}
