const $=selector=>document.querySelector(selector);
const loginTab=$('#loginTab');
const registerTab=$('#registerTab');
const loginForm=$('#loginForm');
const registerForm=$('#registerForm');
const message=$('#authMessage');

function setMode(mode){
  const register=mode==='register';
  loginTab.classList.toggle('active',!register);
  registerTab.classList.toggle('active',register);
  loginForm.classList.toggle('hidden',register);
  registerForm.classList.toggle('hidden',!register);
  $('#authEyebrow').textContent=register?'START YOUR JOURNEY':'WELCOME BACK';
  $('#authTitle').textContent=register?'创建 Northstar 账号':'登录工作空间';
  $('#authSubtitle').textContent=register?'注册后即可使用定价工具并绑定自己的店铺':'使用注册邮箱或手机号继续';
  message.textContent='';
  message.className='auth-message';
}

async function request(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.error||`请求失败（${response.status}）`);
  return data;
}

function setBusy(form,busy){
  const button=form.querySelector('button[type="submit"]');
  button.disabled=busy;
  button.classList.toggle('busy',busy);
}

function showMessage(text,error=false){
  message.textContent=text;
  message.className=`auth-message show${error?' error':' success'}`;
}

loginTab.onclick=()=>setMode('login');
registerTab.onclick=()=>setMode('register');
document.querySelectorAll('[data-toggle-password]').forEach(button=>button.onclick=()=>{
  const input=document.getElementById(button.dataset.togglePassword);
  input.type=input.type==='password'?'text':'password';
  button.textContent=input.type==='password'?'显示':'隐藏';
});

loginForm.onsubmit=async event=>{
  event.preventDefault();setBusy(loginForm,true);showMessage('正在验证账户…');
  try{
    await request('/api/auth/login',{identifier:$('#loginIdentifier').value.trim(),password:$('#loginPassword').value});
    showMessage('登录成功，正在进入工作空间');
    location.replace('/');
  }catch(error){showMessage(error.message,true)}finally{setBusy(loginForm,false)}
};

registerForm.onsubmit=async event=>{
  event.preventDefault();
  if($('#registerPassword').value!==$('#registerConfirm').value){showMessage('两次输入的密码不一致',true);return}
  setBusy(registerForm,true);showMessage('正在创建安全账户…');
  try{
    await request('/api/auth/register',{display_name:$('#registerName').value.trim(),identifier:$('#registerIdentifier').value.trim(),password:$('#registerPassword').value});
    showMessage('注册成功，正在进入定价工作空间');
    location.replace('/');
  }catch(error){showMessage(error.message,true)}finally{setBusy(registerForm,false)}
};

fetch('/api/auth/session').then(response=>response.json()).then(data=>{if(data.authenticated)location.replace('/')}).catch(()=>{});
