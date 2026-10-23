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
    // the demo: the page after a Tuner Mode session, with its history
    assert.match(await page.locator('#connBanner').textContent(),/Demo/);
    assert.equal(await page.locator('#startBtn').isDisabled(),true);
    assert.equal(await page.locator('#pTitle').textContent(),'Chordata - Tuner Mode: 110.4 Hz');
    assert.equal(await page.locator('#beltView .hist li').count(),9);
    assert.equal(await page.locator('#beltView .hist li.out').count(),4);
    assert.match(await page.locator('#beltView').textContent(),/110\.4 Hz.*\+0\.4 Hz, loosen.*Average 110\.05 Hz of the last 5/s);
    // the PC layout on a wide screen with a mouse, the phone layout otherwise
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await page.evaluate(()=>{ placeLayout(); return document.documentElement.classList.contains('pc') === pcQuery.matches; }),true);
    for (const width of [390,1280]) {
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
      await page.screenshot({path:'/tmp/chordata-demo-'+width+'.png',fullPage:true});
    }
    const merged=await page.evaluate(()=>{const state={chordata:{state:'idle',belts:{A:{freq:110}},measurement:{stale:true}}}; merge(state,{chordata:{belts:{},measurement:{}}}); return state;});
    assert.deepEqual(merged,{chordata:{state:'idle',belts:{},measurement:{}}});
    // Cue cards: none closes on a timer or on a press on the phone; green stays for the whole plucking step, with the plucks on it
    const cue=(m,extra)=>page.evaluate(([m,extra])=>{ Object.assign(S.chordata,{state:'listening',belt_live:[],measurement:m},extra||{}); cueUpdate(); },[m,extra]);
    const shown=()=>page.evaluate(()=>!$('cue').hidden && $('cueBig').textContent);
    const liveText=()=>page.evaluate(()=>$('cueLive').textContent);
    await cue({belt:'free',phase:'background'});
    await page.locator('#cue').click();
    await page.waitForTimeout(3500);
    assert.match(await shown(),/background noise/i);
    await cue({belt:'free',phase:'listening'},{belts:{target:110,tol:.25}});
    await page.waitForTimeout(3500);
    assert.match(await shown(),/Pluck either belt/);
    // a pluck left out doesn't replace the number: the newest clear one stays
    await cue({belt:'free',phase:'listening'},{belt_live:[{freq:114.6,ok:true,t:1},{freq:90,ok:false,why:'too weak',t:2}]});
    assert.match(await shown(),/Pluck either belt/);
    assert.match(await liveText(),/114\.6 Hz.*\+4\.6 Hz, loosen/);
    await page.locator('#cue').click();
    assert.match(await shown(),/Pluck either belt/);
    await cue({belt:'tap',phase:'listening',expected:3},{tap_live:[{ok:true},{ok:false,why:'another spot'}]});
    await page.waitForTimeout(400);
    await page.locator('#cue').click();
    assert.match(await shown(),/^Tap your toolhead$/);
    assert.equal(await liveText(),'\u2713\u2717\u25CB\u25CB');
    await cue({belt:'free',phase:'listening'},{belts:{target:110,tol:.25},belt_live:[{freq:111.0,ok:true}]});
    await page.waitForTimeout(400);
    assert.match(await liveText(),/111\.0 Hz.*\+1\.0 Hz, loosen.*Done.*Reset history/);
    // built once: Done and Reset history stay the same elements across renders (a tap never misses);
    // only the box around the number lights up, for a moment, on each new pluck heard; no card flashes
    await page.evaluate(()=>{ window.__btns=[...document.querySelectorAll('#cueLive button')];
      S.chordata.history=[{n:1,freq:111,ok:true,why:'',at:1},{n:2,freq:null,ok:false,why:'a knock on the toolhead',at:2}];
      S.chordata.belt_heard=(S.chordata.belt_heard||0)+1; cueUpdate(); });
    assert.equal(await page.evaluate(()=>document.querySelector('#cueLive .lbox').classList.contains('lit')),true);
    await page.waitForTimeout(700);
    assert.equal(await page.evaluate(()=>document.querySelector('#cueLive .lbox').classList.contains('lit')),false);
    assert.equal(await page.evaluate(()=>{ cueUpdate(); render(); return [...document.querySelectorAll('#cueLive button')].every((b,i)=>b===window.__btns[i]); }),true);
    assert.equal(await page.evaluate(()=>document.getElementById('cue').className.includes('inv')),false);
    // the history: newest at the top, a refused pluck greyed with why
    assert.deepEqual(await page.locator('#cueLive .hist li').allTextContents(),['#2--a knock on the toolhead','#1111.0 Hz']);
    assert.equal(await page.locator('#cueLive .hist li.out').count(),1);
    // a refused pluck that was the plucker's doing gets a word after its reason; a fan's doesn't
    await page.evaluate(()=>{ S.chordata.history=S.chordata.history.concat([{n:3,freq:90,ok:false,why:'too weak',at:3},
      {n:4,freq:null,ok:false,why:'steady hum, not a pluck',at:4},{n:6,freq:null,ok:false,why:'two plucks at once',at:6}]); render(); });
    assert.deepEqual((await page.locator('#cueLive .hist li .why').allTextContents()).slice(0,3),
      ['two plucks at once \u00b7 git good','steady hum, not a pluck','too weak \u00b7 skill issue']);
    // Reset history goes to the plugin through Moonraker (G-code is busy listening)
    await page.evaluate(()=>{ window.__rpc=[]; rpc=(m)=>{ window.__rpc.push(m); return Promise.resolve({}); }; });
    await page.locator('#cueLive button',{hasText:'Reset history'}).click();
    assert.deepEqual(await page.evaluate(()=>window.__rpc),['server.chordata.history_reset']);
    // a double tap's note shows as a notice over the card
    await page.evaluate(()=>{ S.chordata.history_note={text:'History cleared (double tap)',at:5}; render(); });
    assert.match(await page.locator('#toast').textContent(),/double tap/);
    // and the big number goes blank until the next pluck
    assert.match(await liveText(),/^\.\.\./);
    // the +-: how far one pluck can be trusted, from the run of clear plucks just before it lying within
    // twice the tolerance of each other (a pluck of the other belt ends the run; readings exactly alike
    // give none: an artefact repeating, not a sure reading); the shown pluck alone
    // says how far from the target
    await page.evaluate(()=>{ S.chordata.history=[[1,110.4],[2,104.0],[3,110.15],[4,110.1],[5,110.3],[6,110.2]].map(([n,f])=>({n,freq:f,ok:true,why:'',at:n}));
      S.chordata.belt_live=[{freq:110.2,ok:true,t:6,final:true}]; render(); });
    assert.match(await liveText(),/110\.2 Hz.*On target.*Average 110\.19 Hz of the last 4 \(within 1 Hz\).*± 0\.3 Hz per pluck/);
    assert.deepEqual(await page.locator('#cueLive .hist li .u').allTextContents(),["± 0.3"]);
    assert.equal(await page.evaluate(()=>JSON.stringify([u1([90,90.1]),u1([89.67,89.67,89.67])])),'[null,null]');
    await page.evaluate(()=>{ S.chordata.history=S.chordata.history.concat([[7,108.9]].map(([n,f])=>({n,freq:f,ok:true,why:'',at:n})));
      S.chordata.belt_live=[{freq:108.9,ok:true,t:7,final:true}]; render(); });
    assert.match(await liveText(),/-1\.1 Hz, tighten/);
    assert.doesNotMatch(await liveText(),/Average|per pluck/);
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
    const showSetup=()=>live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 110 Hz',
      'action:prompt_button_group_start','action:prompt_button -5 Hz|_CHORDATA_BELTS DT=-5|secondary','action:prompt_button_group_end',
      'action:prompt_button Go to belt tensioning position|_CHORDATA_BELTS STEP=go|secondary',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary',
      'action:prompt_footer_button Close Chordata|RESPOND TYPE=command MSG=action:prompt_end|secondary','action:prompt_show']) handleLine('// '+l); });
    await showSetup();
    // a target typed but not Set goes with the next setup button, so it isn't lost
    await live.locator('#targetHz').fill('125');
    await live.locator('#pBody button', {hasText:'Go to belt tensioning position'}).click();
    await live.waitForFunction(()=>window.__calls.some(r=>r.method==='printer.gcode.script'&&/_CHORDATA_BELTS STEP=go HZ=125\b/.test(r.params.script)));
    await live.evaluate(()=>{ handleLine('// action:prompt_end'); });
    await showSetup();
    assert.equal(await live.locator('#pTitle').textContent(),'Set Target Frequency');
    assert.deepEqual(await live.locator('#pFooter button').allTextContents(),['Back','Close Chordata']);
    assert.equal(await live.locator('#targetHz').inputValue(),'110');
    // a typed target goes to the printer as HZ=, rounded to whole Hz
    await live.locator('#targetHz').fill('118.4');
    await live.locator('#prompt form.ptarget button').click();
    await live.waitForFunction(()=>window.__calls.some(r=>r.method==='printer.gcode.script'&&/_CHORDATA_BELTS HZ=118\b/.test(r.params.script)));
    const order=await live.evaluate(()=>[...document.querySelectorAll('#pBody > *')].map(e=>e.id||e.className||e.tagName));
    assert.deepEqual(order,['ptarget','pbuttons','setupJog','pbuttons','pbuttons']);   // no -5..+5 buttons: the box   // the last: How it works
    assert.equal(await live.locator('#prompt .howlink').count(),1);
    // a target the accelerometer can't hear: red text and a notice, no full-screen card
    await live.evaluate(()=>{ for (const l of ['action:prompt_begin Set Target Frequency','action:prompt_text Target Frequency: 160 Hz',
      'action:prompt_text Accelerometer: LIS3DH, frequency range 0-149 Hz','action:prompt_text Error: frequency target out of range',
      'action:prompt_button Save belt tensioning position|_CHORDATA_BELTS STEP=save|secondary','action:prompt_show']) handleLine('// '+l); });
    assert.equal(await live.locator('#prompt .perr').textContent(),'Error: frequency target out of range');
    assert.equal(await live.locator('#cue').isVisible(),false);
    assert.match(await live.locator('#toast').textContent(),/frequency target out of range/);
    assert.equal(await live.locator('#prompt .howlink').count(),1);    // still there without Start
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
    // Back to Mainsail / Fluidd: the printer's web interface (no same-site page opened Chordata here)
    assert.equal(await live.locator('#backUi').getAttribute('href'),'/');
    assert.equal(await live.locator('#backUi').isVisible(),true);
    assert.equal(await live.locator('#exitBtn').textContent(),'Close Chordata');
    // How it works is its own page, and Back returns
    await live.evaluate(()=>{ handleLine('// action:prompt_end'); });
    await live.waitForTimeout(500);
    await live.locator('#howBtn').click();
    await live.waitForFunction(()=>!document.getElementById('how').hidden);
    // short numbered steps, each with its icon; the details behind More
    assert.equal(await live.locator('#how .manual li').count(),7);
    assert.equal(await live.locator('#how .manual li svg').count(),7);
    assert.equal(await live.locator('#how details summary').textContent(),'More');
    assert.equal(await live.locator('#home').isVisible(),false);
    await live.locator('#howBack').click();
    await live.waitForFunction(()=>!document.getElementById('home').hidden);
    assert.equal(await live.locator('#authPanel').isVisible(),false);   // only when Moonraker refuses the page
    await live.evaluate(()=>authBanner());
    assert.equal(await live.locator('#authPanel').isVisible(),true);
    await live.locator('#moonrakerKey').fill('test-moonraker-placeholder');
    await live.locator('#authForm button').click();
    await live.waitForFunction(()=>window.__calls.some(r=>r.method==='server.connection.identify'));
    assert.equal(await live.locator('#moonrakerKey').inputValue(),'');
    assert.equal(await live.evaluate(()=>localStorage.getItem('moonrakerKey')),null);
    // the PC layout (main on a PC, or ?layout=pc): Mainsail's pop-up look, title bar with an X, no sideways scrolling
    const pc=await browser.newPage();
    pc.on('pageerror',e=>errors.push(e.message));
    await pc.addInitScript(()=>{ window.WebSocket=class { constructor(){throw Error('Demo must not connect');} }; });
    await pc.setViewportSize({width:1280,height:900});
    await pc.goto('file://'+path.resolve(__dirname,'../web/index.html')+'?demo&layout=pc');
    assert.equal(await pc.evaluate(()=>document.documentElement.classList.contains('pc')),true);
    assert.equal(await pc.locator('#pClose').isVisible(),true);
    assert.equal(await pc.locator('#pTitle').textContent(),'Chordata - Tuner Mode: 110.4 Hz');
    assert.equal(await pc.locator('#pTitle').isVisible(),true);
    assert.equal(await pc.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
    await pc.screenshot({path:'/tmp/chordata-pc-1280.png',fullPage:true});
    await pc.evaluate(()=>{ S.chordata.state='listening'; S.chordata.measurement={belt:'free',phase:'listening'}; cueUpdate(); });
    await pc.waitForTimeout(400);
    await pc.screenshot({path:'/tmp/chordata-pc-cue.png'});
    await pc.close();
    // the screen stays on: a press starts the keep-awake video on a plain-http page (no wake lock there)
    await live.evaluate(()=>{ keepAwake(); });
    await live.waitForTimeout(300);
    assert.equal(await live.evaluate(()=>!!document.querySelector('video[aria-hidden=true]')),true);
    assert.deepEqual(errors,[]);
    console.log('Web checks passed: offline demo, live replacement, rejected plucks, Tuner history and stable buttons, How it works steps, phone and PC layouts, keep-awake, no JS errors.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
