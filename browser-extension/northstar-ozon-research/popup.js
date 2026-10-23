const $=selector=>document.querySelector(selector);
const status=(message,type='')=>{$('#status').textContent=message;$('#status').className=type};
function normalizedUrl(value){const url=new URL(value);if(url.protocol!=='https:')throw new Error('ERP 必须使用 HTTPS');return url.origin}
function originPattern(origin){return `${origin}/*`}

async function erpCall(origin,path,options={}){
  const tabs=await chrome.tabs.query({url:originPattern(origin)});
  if(!tabs.length)throw new Error('请先打开并登录 Northstar ERP 标签页');
  const [{result}]=await chrome.scripting.executeScript({target:{tabId:tabs[0].id},func:async(path,options)=>{const response=await fetch(path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);return data},args:[path,options]});
  return result;
}

async function readRules(origin){const data=await erpCall(origin,'/api/research');const rules=data.rules;if(!rules)throw new Error('请先在 ERP 的“Ozon 选品”页面保存选品规则');$('#ruleInfo').innerHTML=`<b>选品规则</b><p>${rules.date_from} 至 ${rules.date_to} · 最多 ${rules.max_candidates} 个${rules.category?` · ${rules.category}`:''}</p>`;return rules}

async function sync(){
  const button=$('#syncBtn');button.disabled=true;
  try{
    const origin=normalizedUrl($('#erpUrl').value.trim());
    const granted=await chrome.permissions.request({origins:[originPattern(origin)]});
    if(!granted)throw new Error('未授权插件访问 ERP 网址');
    await chrome.storage.local.set({erpUrl:origin});
    const rules=await readRules(origin);
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!tab?.url?.startsWith('https://seller.ozon.ru/'))throw new Error('当前标签页不是 Ozon Seller 后台');
    status('正在滚动并读取 Ozon 商品…');
    const response=await chrome.tabs.sendMessage(tab.id,{type:'NORTHSTAR_SCAN',rules,autoScroll:$('#autoScroll').checked,limit:Number($('#scanLimit').value)});
    if(!response?.ok)throw new Error(response?.error||'无法读取 Ozon 页面');
    if(!response.items.length)throw new Error('当前页面没有识别到商品行，请打开“分析 → Ozon 上的商品”结果表格');
    status(`已识别 ${response.items.length} 个，正在按 ERP 规则筛选…`);
    const result=await erpCall(origin,'/api/research/ingest',{method:'POST',body:JSON.stringify({items:response.items})});
    status(`同步完成：进入候选池 ${result.accepted} 个，规则排除 ${result.rejected} 个`, 'success');
  }catch(error){status(error.message||String(error),'error')}finally{button.disabled=false}
}

document.addEventListener('DOMContentLoaded',async()=>{const saved=await chrome.storage.local.get('erpUrl');$('#erpUrl').value=saved.erpUrl||'';$('#syncBtn').onclick=sync;if(saved.erpUrl){try{const origin=normalizedUrl(saved.erpUrl);const has=await chrome.permissions.contains({origins:[originPattern(origin)]});if(has)await readRules(origin)}catch(error){status(error.message,'error')}}});
