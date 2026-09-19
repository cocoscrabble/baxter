const fs = require("node:fs");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const stylesheet = fs.readFileSync(
  path.join(__dirname, "../../static/css/style.css"),
  "utf8",
);

const mobileWidths = [320, 375, 390, 430];

async function renderPublishedPairings(page) {
  await page.setContent(`
    <!doctype html>
    <html>
      <head><style>${stylesheet}</style></head>
      <body>
        <main class="app-main">
          <div class="page">
            <h1>Pairings: Division 1</h1>
            <div id="pairings-content">
              <div class="round-tabs">
                <a class="round-tab round-tab-finished">4</a>
                <a class="round-tab round-tab-finished">5</a>
                <a class="round-tab round-tab-finished">6</a>
                <a class="round-tab round-tab-finished active">7</a>
              </div>
              <section class="round-section">
                <h2>Round 7</h2>
                <table class="published-pairings-table">
                  <thead>
                    <tr>
                      <th>Table</th>
                      <th>1st Player</th>
                      <th>2nd Player</th>
                      <th class="no-print"></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td class="pairing-table-number">12</td>
                      <td class="pairing-player pairing-first">Christopher Alexander-Lewis (#12)</td>
                      <td class="pairing-player pairing-second">Jennifer Clinchy-Smith (#7)</td>
                      <td class="pairing-action no-print"><a class="btn-primary">Submit result</a></td>
                    </tr>
                    <tr>
                      <td class="pairing-table-number">13</td>
                      <td class="pairing-player pairing-first">Alec Sjöholm (#1)</td>
                      <td class="pairing-player pairing-second">Dean Saldanha (#3)</td>
                      <td class="pairing-action num">481–372</td>
                    </tr>
                  </tbody>
                </table>
              </section>
            </div>
          </div>
        </main>
      </body>
    </html>`);
}

for (const width of mobileWidths) {
  test(`published pairings are legible without horizontal overflow at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await renderPublishedPairings(page);

    if (process.env.MOBILE_SCREENSHOT_DIR && width === 390) {
      await page.screenshot({
        path: path.join(process.env.MOBILE_SCREENSHOT_DIR, "mobile-pairings-after.png"),
        fullPage: true,
      });
    }

    const dimensions = await page.locator(".published-pairings-table").evaluate((table) => ({
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      tableRight: table.getBoundingClientRect().right,
      actionRight: table.querySelector(".pairing-action").getBoundingClientRect().right,
    }));
    expect(dimensions.pageWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
    expect(dimensions.tableRight).toBeLessThanOrEqual(dimensions.viewportWidth);
    expect(dimensions.actionRight).toBeLessThanOrEqual(dimensions.viewportWidth);

    const rows = page.locator(".published-pairings-table tbody tr");
    await expect(rows).toHaveCount(2);
    for (const row of await rows.all()) {
      const cells = await row.locator("td").evaluateAll((elements) =>
        elements.map((element) => ({
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
        })),
      );
      expect(cells.every((cell) => cell.scrollWidth <= cell.clientWidth)).toBe(true);
    }

  });
}
