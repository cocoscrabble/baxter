const fs = require('node:fs');
const path = require('node:path');
const {expect, test} = require('@playwright/test');
const root = path.resolve(__dirname, '../..');

for (const width of [390, 1100]) {
  test(`editable grids use page scrolling and keep lower rows editable at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height:800});
    await page.route('https://baxter.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) {
        const rel = url.pathname.slice(8);
        const file = ['static','editgrid/static','tournaments/static','node_modules']
          .map(dir=>path.join(root,dir,rel)).find(fs.existsSync);
        if (file) return route.fulfill({path:file});
      }
      return route.fulfill({contentType:'text/html',body:`
        <link rel="stylesheet" href="/static/tabulator-tables/dist/css/tabulator.min.css">
        <link rel="stylesheet" href="/static/tournaments/css/tabulator_overrides.css">
        <script src="/static/tabulator-tables/dist/js/tabulator.min.js"></script>
        <button id="save">Save</button><div id="grid"></div>
        <script type="module">
          import {TABLE_DEFAULTS} from '/static/editgrid/js/table_helpers.js';
          window.grid = new Tabulator('#grid', {...TABLE_DEFAULTS,
            data:Array.from({length:60},(_,i)=>({id:i+1,name:'Player '+(i+1)})),
            columns:[{field:'id',title:'Seed',width:80},{field:'name',title:'Name',editor:'input'}]});
          document.querySelector('#save').onclick=()=>window.saved=grid.getData();
        </script>`});
    });
    await page.goto('https://baxter.test/grid');
    await expect(page.locator('.tabulator-row')).toHaveCount(60);
    const size = await page.locator('.tabulator-tableholder').evaluate(el=>({client:el.clientHeight,scroll:el.scrollHeight}));
    expect(size.scroll).toBeLessThanOrEqual(size.client+1);
    const last = page.locator('.tabulator-row').last().locator('[tabulator-field="name"]');
    await last.scrollIntoViewIfNeeded();
    await expect(last).toBeInViewport();
    await last.dblclick();
    await last.locator('input').fill('Updated player');
    await last.locator('input').press('Enter');
    await page.getByRole('button',{name:'Save'}).click();
    expect(await page.evaluate(()=>window.saved.at(-1).name)).toBe('Updated player');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  });
}
