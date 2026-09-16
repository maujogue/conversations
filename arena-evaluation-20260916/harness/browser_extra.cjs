const {chromium}=require('/tmp/arena-review-UuyMR9NE/repo/src/frontend/node_modules/playwright');
const fs=require('node:fs');
const ROOT='/tmp/arena-review-UuyMR9NE';
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
 const auth=JSON.parse(fs.readFileSync(`${ROOT}/artifacts/live-auth.json`));
 const context=await browser.newContext({viewport:{width:390,height:844},locale:'fr-FR',isMobile:true,hasTouch:true});
 await context.addCookies([{name:auth.cookie_name,value:auth.cookie_value,domain:'127.0.0.1',path:'/'},{name:'csrftoken',value:'abcdefghijklmnopqrstuvwx12345678',domain:'127.0.0.1',path:'/'}]);
 const page=await context.newPage();
 await page.goto('http://127.0.0.1:23000/',{waitUntil:'networkidle'});
 await page.locator('textarea').fill('Résume ce texte : la réunion est reportée à vendredi.');
 await page.locator('textarea').press('Enter');
 await page.waitForFunction(()=>{const b=document.querySelectorAll('.arena-vote-button');return b.length===2&&[...b].every(x=>!x.disabled)},null,{timeout:30000});
 await page.waitForTimeout(500);
 await page.screenshot({path:`${ROOT}/artifacts/arena-mobile-verified.png`,fullPage:true});
 const metrics=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,buttons:[...document.querySelectorAll('.arena-vote-button')].map(x=>({text:x.innerText,rect:x.getBoundingClientRect().toJSON()})),sides:[...document.querySelectorAll('[data-testid^="arena-side-"]')].map(x=>({rect:x.getBoundingClientRect().toJSON()}))}));
 await page.locator('.arena-vote-button').first().click();
 await page.locator('[data-testid="arena-turn"]').waitFor({state:'detached'});
 metrics.canVote=true;
 fs.writeFileSync(`${ROOT}/artifacts/browser-mobile.json`,JSON.stringify(metrics,null,2));
 console.log(JSON.stringify(metrics,null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
