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
    // Cue cards: none closes on a timer or on a press on the phone; green stays for the whole plucking step, with the plucks on it
    const cue=(m,extra)=>page.evaluate(([m,extra])=>{ Object.assign(S.chordata,{state:'listening',belt_live:[],measurement:m},extra||{}); cueUpdate(); },[m,extra]);
    const shown=()=>page.evaluate(()=>!$('cue').hidden && $('cueBig').textContent);
    const liveText=()=>page.evaluate(()=>$('cueLive').textContent);
    await cue({belt:'A',phase:'background'});
    await page.locator('#cue').click();
    await page.waitForTimeout(3500);
    assert.match(await shown(),/background noise/i);
    await cue({belt:'A',phase:'listening',heard:0});
    await page.waitForTimeout(3500);
    assert.match(await shown(),/Pluck belt A/);
    await cue({belt:'A',phase:'listening',heard:2},{belt_live:[{freq:114.6,ok:true},{freq:90,ok:false,why:'too weak'}]});
    assert.match(await shown(),/Pluck belt A/);
    assert.match(await liveText(),/90\.0 Hz.*1 of \d counted.*too weak/);
    await page.locator('#cue').click();
    assert.match(await shown(),/Pluck belt A/);
    await cue({belt:'both',phase:'listening',step:'tap'});
    await page.waitForTimeout(400);
    assert.match(await shown(),/Tap your spot/);
    assert.equal(await liveText(),'');
    await cue({belt:'both',phase:'listening',step:'B'},{belts:{target:110,plucks:5,A:{freq:114.6}}});
    await page.waitForTimeout(400);
    assert.match(await shown(),/Pluck belt B/);
    assert.match(await liveText(),/Belt A: 114\.6 Hz/);
    await cue({belt:'tap',phase:'listening',expected:3},{tap_live:[{ok:true},{ok:false,why:'another spot'}]});
    await page.waitForTimeout(400);
    await page.locator('#cue').click();
    assert.match(await shown(),/Tap your spot/);
    assert.equal(await liveText(),'\u2713\u2717\u25CB\u25CB');
    await cue({belt:'free',phase:'listening'},{belts:{target:110,tol:.25,plucks:5},belt_live:[{freq:111.0,ok:true}]});
    await page.waitForTimeout(400);
    assert.match(await liveText(),/111\.0 Hz.*\+1\.0 Hz, loosen.*Done/);
    await page.evaluate(()=>{ S.chordata.state='idle'; S.chordata.measurement={phase:'idle',belt:null}; cueUpdate(); });
    assert.equal(await shown(),false);
    const live=await browser.newPage();
    live.on('pageerror',e=>errors.push(e.message));
    await live.addInitScript(()=>{
      window.__calls=[];
      window.WebSocket=class {
        constructor(){this.readyState=1;setTimeout(()=>this.onopen(),0);}
        send(raw){const request=JSON.parse(raw); window.__calls.push(request); let result={};
          if(request.method==='server.info')result={klippy_state:'ready'};
          if(request.method==='printer.objects.subscribe')result={status:{chordata:{state:'idle',tools:{accel:true,corexy:true},belts:{target:110,A:{plucks:[{freq:110,ok:true}]}}}}};
          const reply=()=>this.onmessage({data:JSON.stringify({id:request.id,result})});
          if(request.method==='printer.gcode.script'&&request.params.script==='G28'){window.__releaseHome=reply;return;}
          setTimeout(reply,0);
        }
      };
    });
    await live.goto('file://'+path.resolve(__dirname,'../web/index.html'));
    // Set Target Frequency: the target big, then the Hz buttons, Go, the pad, Save; no "Chordata - " title on the page
    await live.waitForFunction(()=>typeof handleLine==='function' && connected);
    await live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 110 Hz',
      'action:prompt_button_group_start','action:prompt_button -5 Hz|_CHORDATA_BELTS DT=-5|secondary','action:prompt_button_group_end',
      'action:prompt_button Go to belt tensioning position|_CHORDATA_BELTS STEP=go|secondary',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary','action:prompt_show']) handleLine('// '+l); });
    assert.equal(await live.locator('#pTitle').textContent(),'Set Target Frequency');
    assert.equal(await live.locator('#prompt h2.ptarget').textContent(),'Target Frequency: 110 Hz');
    const order=await live.evaluate(()=>[...document.querySelectorAll('#pBody > *')].map(e=>e.id||e.className||e.tagName));
    assert.deepEqual(order,['ptarget','pgroup','pbuttons','setupJog','pbuttons','pbuttons']);   // the last: How it works
    assert.equal(await live.locator('#prompt .howlink').count(),1);
    // a target the accelerometer can't hear: red text, and the screen flashes red with it
    await live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 160 Hz',
      'action:prompt_text Accelerometer: LIS3DH, frequency range 0-149 Hz','action:prompt_text Error: frequency target out of range',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary','action:prompt_show']) handleLine('// '+l); });
    assert.equal(await live.locator('#prompt .perr').textContent(),'Error: frequency target out of range');
    assert.equal(await live.locator('#cue').isVisible(),true);
    assert.match(await live.locator('#cueBig').textContent(),/frequency target out of range/);
    assert.equal(await live.locator('#prompt .howlink').count(),1);    // still there without Start
    await live.waitForFunction(()=>document.getElementById('cue').hidden,null,{timeout:5000});
    await live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 110 Hz',
      'action:prompt_button Go to belt tensioning position|_CHORDATA_BELTS STEP=go|secondary',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary','action:prompt_show']) handleLine('// '+l); });
    // A long homing: no request timeout, and no jog pad until our G28 returns (X/Y report homed before Z)
    const homeBtn=live.locator('#setupJog button',{hasText:/^Home$/});
    await homeBtn.click();
    await live.waitForFunction(()=>typeof window.__releaseHome==='function');
    assert.equal(await live.evaluate(()=>[...pending.values()].every(p=>p.timer===null)),true);
    await live.evaluate(()=>{ S.toolhead={homed_axes:'xy',position:[10,20,0,0]}; render(); });
    assert.match(await live.locator('#setupJog').textContent(),/Stand by, homing\./);
    assert.equal(await live.locator('#setupJog .pad').isVisible(),false);
    await live.evaluate(()=>window.__releaseHome());
    await live.waitForFunction(()=>document.querySelector('#setupJog .pad').offsetParent!==null);
    // Reloaded mid-homing: the printer is busy and Z is not homed yet
    await live.evaluate(()=>{ S.idle_timeout={state:'Printing'}; S.toolhead={homed_axes:'xy',position:[10,20,0,0]}; render(); });
    assert.match(await live.locator('#setupJog').textContent(),/Stand by, homing\./);
    await live.evaluate(()=>{ S.idle_timeout={state:'Ready'}; S.toolhead={homed_axes:'xyz',position:[10,20,0,0]}; render(); });
    assert.equal(await live.locator('#setupJog .pad').isVisible(),true);
    // a typed coordinate survives status updates, and Move sends an absolute move
    await live.locator('#setupJog input[name=x]').fill('176.5');
    await live.evaluate(()=>{ S.toolhead={homed_axes:'xyz',position:[11,20,0,0]}; render(); });
    assert.equal(await live.locator('#setupJog input[name=x]').inputValue(),'176.5');
    await live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 111 Hz',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary','action:prompt_show']) handleLine('// '+l); });
    assert.equal(await live.locator('#setupJog input[name=x]').inputValue(),'176.5');   // kept when the step is shown again
    await live.locator('#setupJog input[name=y]').fill('112.5');
    await live.locator('#setupJog .xy button').click();
    await live.waitForFunction(()=>window.__calls.some(r=>r.method==='printer.gcode.script'&&/G90\nG1 X176.5 Y112.5/.test(r.params.script)));
    // How it works is its own page, and Back returns
    await live.evaluate(()=>{ handleLine('// action:prompt_end'); });
    await live.waitForTimeout(500);
    await live.locator('#howBtn').click();
    await live.waitForFunction(()=>!document.getElementById('how').hidden);
    assert.equal(await live.locator('#home').isVisible(),false);
    await live.locator('#howBack').click();
    await live.waitForFunction(()=>!document.getElementById('home').hidden);
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
