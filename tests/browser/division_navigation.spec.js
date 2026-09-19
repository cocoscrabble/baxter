const fs = require("node:fs");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const stylesheet = fs.readFileSync(
  path.join(__dirname, "../../static/css/style.css"),
  "utf8",
);

async function renderDivisionStandings(page) {
  await page.setContent(`
    <!doctype html>
    <html>
      <head><style>${stylesheet}</style></head>
      <body>
        <header class="app-header">
          <nav class="navbar">
            <div class="nav-top">
              <div class="nav-left">
                <span class="nav-logo-link"><span class="nav-logo" style="display:inline-block;width:28px"></span></span>
                <a class="nav-brand">Baxter</a>
                <span class="nav-sep">/</span>
                <a>Vancouver End of Summer</a>
                <span class="nav-sep">/</span>
                <a>Division 1</a>
              </div>
              <div class="nav-right"><a>Login</a></div>
            </div>
            <div class="nav-tabs">
              <a class="nav-tab">Entrants</a>
              <a class="nav-tab active">Standings</a>
              <a class="nav-tab">Pairings</a>
              <a class="nav-tab">Results</a>
              <a class="nav-tab">Ratings</a>
            </div>
          </nav>
        </header>
        <main class="app-main">
          <div class="page">
            <h1>Standings: Division 1</h1>
            <div class="round-tabs">
              <a class="round-tab round-tab-finished">1</a>
              <a class="round-tab round-tab-finished">2</a>
              <a class="round-tab round-tab-finished active">3</a>
            </div>
            <h2>After round 3</h2>
            <div class="table-scroll table-narrow">
              <table class="standings-table">
                <thead>
                  <tr>
                    <th class="num standings-rank">Rank</th>
                    <th class="standings-player">Player</th>
                    <th class="num standings-record">W-L</th>
                    <th class="num standings-spread">Spread</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td class="num standings-rank">1.</td>
                    <td class="standings-player"><span class="standings-player-name">Alec Sjöholm</span> <span class="standings-player-number">(#1)</span></td>
                    <td class="num standings-record">3-0</td>
                    <td class="num standings-spread">905</td>
                  </tr>
                  <tr>
                    <td class="num standings-rank">2.</td>
                    <td class="standings-player"><span class="standings-player-name">Jennifer Clinchy</span> <span class="standings-player-number">(#7)</span></td>
                    <td class="num standings-record">2-1</td>
                    <td class="num standings-spread">558</td>
                  </tr>
                  <tr>
                    <td class="num standings-rank">3.</td>
                    <td class="standings-player"><span class="standings-player-name">Christopher Alexander-Lewis</span> <span class="standings-player-number">(#12)</span></td>
                    <td class="num standings-record">2-1</td>
                    <td class="num standings-spread">175</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </main>
      </body>
    </html>`);
}

async function textRect(locator) {
  return locator.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const rect = range.getBoundingClientRect();
    return { left: rect.left, right: rect.right };
  });
}

test("mobile division header and standings remain readable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await renderDivisionStandings(page);

  if (process.env.MOBILE_SCREENSHOT_DIR) {
    await page.screenshot({
      path: path.join(process.env.MOBILE_SCREENSHOT_DIR, "mobile-division-after.png"),
      fullPage: true,
    });
  }

  const layout = await page.locator("body").evaluate(() => {
    const left = document.querySelector(".nav-left").getBoundingClientRect();
    const right = document.querySelector(".nav-right").getBoundingClientRect();
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      navigationSeparated: left.right <= right.left,
    };
  });
  expect(layout.pageWidth).toBeLessThanOrEqual(layout.viewportWidth);
  expect(layout.navigationSeparated).toBe(true);

  const record = await textRect(page.locator("th.standings-record"));
  const spread = await textRect(page.locator("th.standings-spread"));
  expect(spread.left - record.right).toBeGreaterThanOrEqual(8);
});
