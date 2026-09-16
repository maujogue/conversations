const {chromium}=require('/tmp/arena-review-UuyMR9NE/repo/src/frontend/node_modules/playwright');
const fs=require('node:fs');const ROOT='/tmp/arena-review-UuyMR9NE';
(async()=>{
const browser=await chromium.launch({headless:true});
try {
const auth=JSON.parse(fs.readFileSync(`${ROOT}/artifacts/live-auth.json`));
const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'fr-FR'});
await context.addCookies([{name:auth.cookie_name,value:auth.cookie_value,domain:'127.0.0.1',path:'/'},{name:'csrftoken',value:'abcdefghijklmnopqrstuvwx12345678',domain:'127.0.0.1',path:'/'}]);
const page=await context.newPage();const errors=[],responses=[];
page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.url().includes('/conversation/'))responses.push({status:r.status(),url:r.url()})});
const created=await context.request.post('http://127.0.0.1:28072/api/v1.0/chats/',{data:{title:'QA real API existing conversation'},headers:{'X-CSRFToken':'abcdefghijklmnopqrstuvwx12345678','Origin':'http://127.0.0.1:23001'}});
if(created.status()!==201)throw new Error('Create status '+created.status());
const conversation=await created.json();
await page.goto('http://127.0.0.1:23001/chat/'+conversation.id,{waitUntil:'networkidle'});
const prompt='Cas fictif. Réécris ce message administratif en moins de 100 mots, sans ajouter de nouvelles informations : Madame Martin, votre dossier de formation est incomplet car il manque l’attestation de formation. Merci de la transmettre avant le 21 septembre 2026 à 17 h. Aucune décision d’acceptation n’a encore été prise. Signature : Service formation.';
await page.locator('textarea').fill(prompt);const start=Date.now();await page.locator('textarea').press('Enter');
await page.waitForFunction(()=>{const b=document.querySelectorAll('.arena-vote-button');return b.length===2&&[...b].every(x=>!x.disabled)},null,{timeout:90000});
const f={prompt,comparisonDurationMs:Date.now()-start,url:page.url(),sides:await page.locator('[data-testid^="arena-side-"]').allTextContents(),responses,errors};
await page.screenshot({path:`${ROOT}/artifacts/arena-real-api-existing.png`,fullPage:true});
// Synthetic QA vote verifies persistence; it is not a human preference observation.
fs.writeFileSync(`${ROOT}/artifacts/browser-real-existing.json`,JSON.stringify(f,null,2));
const voteResponse=page.waitForResponse(r=>r.url().includes('/vote/')&&r.request().method()==='POST');
await page.locator('.arena-vote-button').first().click();f.voteHTTP=(await voteResponse).status();
if(f.voteHTTP===200)await page.locator('[data-testid="arena-turn"]').waitFor({state:'detached'});
f.voted=f.voteHTTP===200;await page.reload({waitUntil:'networkidle'});f.persistedText=await page.locator('main').count()?await page.locator('main').innerText():await page.locator('body').innerText();
f.noPendingArena=await page.locator('[data-testid="arena-turn"]').count()===0;
fs.writeFileSync(`${ROOT}/artifacts/browser-real-existing.json`,JSON.stringify(f,null,2));console.log(JSON.stringify(f,null,2));
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
