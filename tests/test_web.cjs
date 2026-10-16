// Run: node tests/test_web.cjs (requires Playwright and its Chromium browser).
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(()=>{ window.__connections=0; window.WebSocket=class { constructor(){window.__connections++;throw Error('Demo must not connect');} }; });
    await page.goto('file://'+path.resolve(__dirname,'../web/index.html')+'?demo');
    assert.equal(await page.evaluate(()=>window.__connections),0);
    assert.equal(await page.locator('#measureRows tr').count(),7);
    assert.match(await page.locator('#connBanner').textContent(),/Demo/);
    assert.equal(await page.locator('#startBtn').isDisabled(),true);
    assert.equal(await page.locator('#trend circle').count(),6);
    assert.equal(await page.locator('#trend path').count(),1);
    assert.equal(await page.evaluate(()=>csvMeasurements().split('\r\n').length),8);
    for (const width of [390,1280]) {
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
      await page.screenshot({path:'/tmp/chordata-demo-'+width+'.png',fullPage:true});
    }
    await page.evaluate(()=>{ S.chordata.state='listening'; S.chordata.belt_now='A'; S.chordata.belt_live=[{freq:111.5,ok:true},{freq:null,ok:false,why:'No signal'}]; renderMeasurements(); });
    assert.equal(await page.locator('#measureRows tr').count(),5);
    assert.match(await page.locator('#measureRows').textContent(),/No signal/);
    const merged=await page.evaluate(()=>{const state={chordata:{state:'idle',belts:{A:{freq:110}},measurement:{stale:true}}}; merge(state,{chordata:{belts:{},measurement:{}}}); return state;});
    assert.deepEqual(merged,{chordata:{state:'idle',belts:{},measurement:{}}});
    // A rejected pluck with no frequency remains in data without inventing a chart point.
    assert.equal(await page.locator('#trend path').count(),0);
    await page.evaluate(()=>{ S.chordata.belt_live[1].why='=unsafe,"quoted"'; });
    assert.match(await page.evaluate(()=>csvMeasurements()),/'=unsafe,""quoted""/);
    const live=await browser.newPage();
    live.on('pageerror',e=>errors.push(e.message));
    await live.addInitScript(()=>{
      window.__calls=[];
      window.WebSocket=class {
        constructor(){this.readyState=1;setTimeout(()=>this.onopen(),0);}
        send(raw){const request=JSON.parse(raw); window.__calls.push(request); let result={};
          if(request.method==='server.info')result={klippy_state:'ready'};
          if(request.method==='printer.objects.subscribe')result={status:{chordata:{state:'idle',tools:{accel:true,corexy:true},belts:{target:110,A:{plucks:[{freq:110,ok:true}]}}}}};
          if(request.method==='access.chordata_ai')result={enabled:false,provider:'laya',endpoint:'',model:'auto',min_confidence:.7};
          if(request.method==='server.chordata.evaluate')result={status:'abstained',message:'Not enough reliable plucks.'};
          setTimeout(()=>this.onmessage({data:JSON.stringify({id:request.id,result})}),0);
        }
      };
    });
    await live.goto('file://'+path.resolve(__dirname,'../web/index.html'));
    await live.locator('#aiPanel summary').click();
    await live.locator('#aiEnabled').check();
    await live.locator('#aiEndpoint').fill('http://laya.local:8000/v1/systemone');
    await live.locator('#aiKey').fill('test-placeholder');
    await live.locator('#aiSave').click();
    await live.waitForFunction(()=>document.getElementById('aiStatus').textContent.startsWith('Settings saved'));
    assert.equal(await live.locator('#aiKey').inputValue(),'');
    const config=await live.evaluate(()=>window.__calls.find(r=>r.method==='access.chordata_ai' && r.params.action==='configure'));
    assert.equal(config.params.endpoint,'http://laya.local:8000/v1/systemone');
    assert.equal(config.params.api_key,'test-placeholder');
    await live.locator('#aiClearKey').check();
    await live.locator('#aiSave').click();
    await live.waitForFunction(()=>window.__calls.filter(r=>r.method==='access.chordata_ai' && r.params.action==='configure').length===2);
    assert.equal(await live.evaluate(()=>window.__calls.filter(r=>r.method==='access.chordata_ai' && r.params.action==='configure')[1].params.api_key),'');
    await live.locator('#aiReview').click();
    await live.waitForFunction(()=>document.getElementById('aiStatus').textContent==='Not enough reliable plucks.');
    await live.evaluate(()=>{ S.chordata.state='listening'; render(); });
    assert.match(await live.locator('#aiStatus').textContent(),/Measurements changed/);
    await live.locator('#authPanel summary').click();
    await live.locator('#moonrakerKey').fill('test-moonraker-placeholder');
    await live.locator('#authForm button').click();
    await live.waitForFunction(()=>window.__calls.some(r=>r.method==='server.connection.identify'));
    assert.equal(await live.locator('#moonrakerKey').inputValue(),'');
    assert.equal(await live.evaluate(()=>localStorage.getItem('moonrakerKey')),null);
    assert.deepEqual(errors,[]);
    console.log('Web checks passed: offline demo, live replacement, rejected plucks, CSV escaping, mobile/desktop layout, no JS errors.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
