// npm ci && npx playwright install chromium && node tools/capture-docs.cjs
const { chromium } = require('playwright');
const path = require('node:path');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_EXECUTABLE || undefined});
  try {
    fs.mkdirSync(path.resolve(__dirname,'../docs/images'),{recursive:true});
    const page=await browser.newPage({viewport:{width:1280,height:960},deviceScaleFactor:1});
    await page.goto('file://'+path.resolve(__dirname,'../web/index.html')+'?demo');
    await page.screenshot({path:path.resolve(__dirname,'../docs/images/desktop-demo.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.resolve(__dirname,'../docs/images/mobile-demo.png'),fullPage:true});
    await page.setViewportSize({width:1000,height:1000});
    await page.evaluate(()=>{
      document.getElementById('aiPanel').open=true;
      document.getElementById('aiForm').hidden=false;
      document.getElementById('aiEnabled').checked=true;
      document.getElementById('aiEndpoint').value='http://laya-host:8000/v1/systemone';
      document.getElementById('aiStatus').textContent='Illustration only · example endpoint · no review requested';
    });
    await page.locator('#aiPanel').screenshot({path:path.resolve(__dirname,'../docs/images/ai-settings.png')});
    await page.locator('#measurePanel details').evaluate(e=>e.open=true);
    await page.locator('#measurePanel').screenshot({path:path.resolve(__dirname,'../docs/images/measurement-data.png')});
    console.log('Captured four documentation screenshots from the offline demo.');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1);});
