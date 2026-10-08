const fs = require('node:fs');
const path = require('node:path');
const { expect, test } = require('@playwright/test');

async function editor(page, saveOk) {
  let saved;
  await page.route('https://baxter.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/static/')) {
      const rel = url.pathname.slice(8);
      const file = ['static', 'tournaments/static', 'editgrid/static', 'node_modules']
        .map(root => path.resolve(__dirname, '../..', root, rel)).find(fs.existsSync);
      if (file) return route.fulfill({path: file});
    }
    if (url.pathname === '/save') {
      saved = route.request().postDataJSON();
      return route.fulfill({status: saveOk ? 200 : 400, contentType: 'application/json',
        body: JSON.stringify(saveOk ? {ok: true, version: 1} : {errors: ['Invalid schedule']})});
    }
    if (url.pathname === '/pair-rounds') return route.fulfill({body: '<h1>Pair Rounds</h1>'});
    return route.fulfill({contentType: 'text/html', body: `
      <script src="/static/tabulator-tables/dist/js/tabulator.min.js"></script>
      <select id="pairing-method"><option value="custom">Custom</option></select>
      <span id="method-rounds-controls"><input id="method-total-rounds" value="14"></span>
      <button id="generate-method-btn"></button><span id="method-status"></span>
      <div id="schedule-editor" hidden><div id="pairing-blocks-table"></div>
      <div id="round-pairings-preview-table"></div></div>
      <button id="add-block-btn"></button><button id="rp-save-btn"></button><span id="rp-save-status"></span>
      <script>const pageData = {blocks:[{pairing:'RoundRobin',rounds:5,pair_from:1}], preview:[],
      strategyTypes:[{value:'RoundRobin',label:'Round Robin'}], defaultRounds:{RoundRobin:5},
      csrfToken:'fixture', saveUrl:'/save', pairRoundsUrl:'/pair-rounds'};</script>
      <script type="module" src="/static/tournaments/js/edit_round_pairings.js"></script>`});
  });
  await page.goto('https://baxter.test/editor');
  await expect(page.locator('#schedule-editor')).toBeVisible();
  await expect(page.locator('#pairing-blocks-table .tabulator-row')).toHaveCount(1);
  return () => saved;
}

test('Custom saves its blocks before opening Pair Rounds', async ({page}) => {
  const saved = await editor(page, true);
  await page.getByRole('button', {name: 'Save Schedule and View Pairings'}).click();
  await expect(page).toHaveURL('https://baxter.test/pair-rounds');
  expect(saved().blocks).toEqual([{pairing:'RoundRobin',rounds:5,pair_from:1}]);
});

test('Custom stays in the editor if saving fails', async ({page}) => {
  await editor(page, false);
  await page.getByRole('button', {name: 'Save Schedule and View Pairings'}).click();
  await expect(page.locator('#method-status')).toContainText('Schedule not saved');
  await expect(page).toHaveURL('https://baxter.test/editor');
});
