const fs = require('node:fs');
const path = require('node:path');
const {expect,test} = require('@playwright/test');
const css = fs.readFileSync(path.resolve(__dirname,'../../static/css/style.css'),'utf8');
const template = fs.readFileSync(path.resolve(__dirname,'../../tournaments/templates/tournaments/_published_pairings_table.html'),'utf8');
const colgroup = template.match(/<colgroup>[\s\S]*?<\/colgroup>/)[0];

for (const viewport of [{width:1100,height:800},{width:800,height:1100}]) {
  test(`LIVE rounds align despite different player name lengths at ${viewport.width}px`, async ({page})=>{
    await page.setViewportSize(viewport);
    const table = name => `<table class="published-pairings-table live-table">${colgroup}
      <thead><tr><th>Table</th><th>1st Player</th><th>2nd Player</th><th>Result</th></tr></thead>
      <tbody><tr><td>1</td><td class="pairing-player">${name}</td><td class="pairing-player">Alec Sjöholm (#2)</td><td>400 - 350</td></tr></tbody></table>`;
    await page.setContent(`<style>${css}</style><body class="live-body"><main class="live-page">
      <div id="live-content" style="--live-rows:2;--live-cols:1;--live-col-rows:2;--live-col-ems:50;--live-overhead:11;--live-col-overhead:11">
      ${['Conrad Bassett-Bouchard (#1)','Eric Fox (#6)'].map((name,i)=>`<section class="live-section"><h2>Round ${i+1}</h2><div class="live-single">${table(name)}</div><div class="live-grid">${table(name)}</div></section>`).join('')}
      </div></main></body>`);
    const tables = page.locator(viewport.width>viewport.height?'.live-grid table':'.live-single table');
    const columns = await tables.evaluateAll(tables=>tables.map(t=>[...t.querySelectorAll('th')].map(th=>({x:th.getBoundingClientRect().x,width:th.getBoundingClientRect().width}))));
    expect(columns[0]).toEqual(columns[1]);
    const overflow = await tables.locator('td').evaluateAll(cells=>cells.some(c=>c.scrollWidth>c.clientWidth+1));
    expect(overflow).toBe(false);
  });
}
