const {chromium}=require('/tmp/arena-review-UuyMR9NE/repo/src/frontend/node_modules/playwright');const fs=require('node:fs');const ROOT='/tmp/arena-review-UuyMR9NE';
(async()=>{
const browser=await chromium.launch({headless:true});
try {
 const auth=JSON.parse(fs.readFileSync(`${ROOT}/artifacts/live-auth.json`));
 const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'fr-FR'});
 await context.addCookies([{name:auth.cookie_name,value:auth.cookie_value,domain:'127.0.0.1',path:'/'},{name:'csrftoken',value:'abcdefghijklmnopqrstuvwx12345678',domain:'127.0.0.1',path:'/'}]);
 const page=await context.newPage();const events=[];let drawDone;const drawn=new Promise(r=>drawDone=r);const t=Date.now();
 page.on('response',r=>{if(r.url().includes('/arena/')||r.url().includes('/conversation/')){events.push({ms:Date.now()-t,url:r.url(),status:r.status(),method:r.request().method(),body:r.request().postData()});if(r.url().endsWith('/draw/')&&r.request().method()==='POST')drawDone()}});
 await page.route(/\/api\/v1.0\/chats\/[a-f0-9-]+\/$/,async route=>{if(route.request().method()==='GET')await Promise.race([drawn,new Promise(r=>setTimeout(r,2500))]);await route.continue()});
 await page.goto('http://127.0.0.1:23000/',{waitUntil:'networkidle'});
 await page.locator('textarea').fill('QA première conversation. Réponds brièvement. [SLOW]');await page.locator('textarea').press('Enter');
 await page.waitForTimeout(6000);
 let clicked=false;
 if(await page.locator('.arena-vote-button').count()&&await page.locator('.arena-vote-button').first().isEnabled()){await page.locator('.arena-vote-button').first().click();clicked=true;await page.waitForTimeout(500)}
 await page.screenshot({path:`${ROOT}/artifacts/arena-first-turn-race.png`,fullPage:true});
 const f={description:'Synthetic network ordering: delay chat-detail GET until draw completes; model response slow. No source modified.',url:page.url(),clicked,events,body:await page.locator('body').innerText()};
 fs.writeFileSync(`${ROOT}/artifacts/browser-first-turn-race.json`,JSON.stringify(f,null,2));console.log(JSON.stringify(f,null,2));
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
