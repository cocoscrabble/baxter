const fs = require("node:fs");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const longPlayerName = "Christopher Alexander-Lewis";
const longPlayerLabel = `${longPlayerName} (#12)`;
const mobileWidths = [320, 360, 375, 390, 414, 430, 600];

const stylesheet = fs.readFileSync(
  path.join(__dirname, "../../static/css/style.css"),
  "utf8",
);

function standingsTable({ includeFirsts, includeTies = true }) {
  const firstsHeading = includeFirsts
    ? '<th class="num standings-firsts">Firsts</th>'
    : "";
  const rows = [
    ["Alec Sjöholm", "1", "16-4", "1977", "10", false],
    ["Jennifer Clinchy", "7", includeTies ? "10.5-9.5" : "14-6", "1032", "9", false],
    ["Evans Clinchy", "2", includeTies ? "9.5-10.5" : "13-7", "224", "10", false],
    [longPlayerName, "12", includeTies ? "10.5-3.5" : "10-4", "576", "10", false],
    ["Eric Fox", "5", "10-10", "-42", "10", true],
  ]
    .map(([player, number, score, spread, firsts, dropped]) => `
      <tr>
        <td class="num standings-rank">1</td>
        <td class="standings-player" title="${player} (#${number})${dropped ? " — withdrew" : ""}">
          <span class="standings-player-name">${player}</span>
          <span class="standings-player-number">(#${number})</span>
          ${dropped ? '<span class="tag tag-dropped">withdrew</span>' : ""}
        </td>
        <td class="num standings-record">${score}</td>
        <td class="num standings-spread">${spread}</td>
        ${includeFirsts ? `<td class="num standings-firsts">${firsts}</td>` : ""}
      </tr>`)
    .join("");

  return `
    <div class="table-scroll table-narrow" data-testid="standings-scroll">
      <table class="standings-table">
        <thead>
          <tr>
            <th class="num standings-rank">Rank</th>
            <th class="standings-player">Player</th>
            <th class="num standings-record">W-L</th>
            <th class="num standings-spread">Spread</th>
            ${firstsHeading}
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

async function renderStandings(page, options) {
  await page.setContent(`
    <!doctype html>
    <html>
      <head><style>${stylesheet}</style></head>
      <body>
        <main class="app-main">
          <div class="page">
            <div id="standings-content">
              <div id="round-tab-content">${standingsTable(options)}</div>
            </div>
          </div>
        </main>
      </body>
    </html>`);
}

async function cellLineCounts(locator) {
  return locator.evaluateAll((cells) =>
    cells.map((cell) => {
      const range = document.createRange();
      range.selectNodeContents(cell);
      const lineTops = new Set(
        [...range.getClientRects()]
          .filter((rect) => rect.width > 0 && rect.height > 0)
          .map((rect) => Math.round(rect.top)),
      );
      return {
        classes: [...cell.classList],
        lines: lineTops.size,
        text: cell.textContent.replace(/\s+/g, " ").trim(),
        whiteSpace: getComputedStyle(cell).whiteSpace,
      };
    }),
  );
}

async function expectNumericCellsToFit(page) {
  const cells = await page.locator(".standings-table .num").evaluateAll((elements) =>
    elements.map((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      text: element.textContent.trim(),
    })),
  );
  for (const cell of cells) {
    expect(cell.scrollWidth, cell.text).toBeLessThanOrEqual(cell.clientWidth);
  }
}

for (const width of mobileWidths) {
  for (const includeFirsts of [false, true]) {
    test(`mobile standings fit at ${width}px${includeFirsts ? " with Firsts" : ""}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 812 });
      await renderStandings(page, { includeFirsts });

      const nonPlayerCells = page.locator(
        ".standings-table th:not(.standings-player), .standings-table td:not(.standings-player)",
      );
      const nonPlayerMeasurements = await cellLineCounts(nonPlayerCells);
      expect(nonPlayerMeasurements).not.toHaveLength(0);
      for (const measurement of nonPlayerMeasurements) {
        expect(measurement, measurement.text).toMatchObject({
          lines: 1,
          whiteSpace: "nowrap",
        });
      }
      await expectNumericCellsToFit(page);

      const overflow = await page.getByTestId("standings-scroll").evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
      }));
      expect(overflow.scrollWidth).toBe(overflow.clientWidth);
      expect(overflow.pageWidth).toBeLessThanOrEqual(overflow.viewportWidth);

      const allCellMeasurements = await cellLineCounts(
        page.locator(".standings-table th, .standings-table td"),
      );
      expect(
        allCellMeasurements
          .filter(({ lines }) => lines > 1)
          .every(({ classes }) => classes.includes("standings-player")),
      ).toBe(true);

      const longPlayer = page.locator(`td[title="${longPlayerLabel}"]`);
      const longPlayerFit = await longPlayer.evaluate((cell) => ({
        clientWidth: cell.clientWidth,
        scrollWidth: cell.scrollWidth,
        text: cell.textContent.replace(/\s+/g, " ").trim(),
        textOverflow: getComputedStyle(cell.querySelector(".standings-player-name")).textOverflow,
        whiteSpace: getComputedStyle(cell).whiteSpace,
      }));
      expect(longPlayerFit).toMatchObject({
        text: longPlayerLabel,
        textOverflow: "clip",
        whiteSpace: "normal",
      });
      expect(longPlayerFit.scrollWidth).toBeLessThanOrEqual(longPlayerFit.clientWidth);
      if (width === 320) {
        const [longPlayerMeasurement] = await cellLineCounts(longPlayer);
        expect(longPlayerMeasurement.lines).toBeGreaterThan(1);
      }

      const number = longPlayer.locator(".standings-player-number");
      expect(await number.innerText()).toBe("(#12)");
      const numberFit = await number.evaluate((element) => {
        const numberRect = element.getBoundingClientRect();
        const cellRect = element.closest("td").getBoundingClientRect();
        return {
          fits: numberRect.left >= cellRect.left && numberRect.right <= cellRect.right,
          whiteSpace: getComputedStyle(element).whiteSpace,
        };
      });
      expect(numberFit).toEqual({ fits: true, whiteSpace: "nowrap" });

      const dropped = page.locator('td[title="Eric Fox (#5) — withdrew"]');
      const droppedTagFit = await dropped.locator(".tag-dropped").evaluate((tag) => {
        const tagRect = tag.getBoundingClientRect();
        const cellRect = tag.closest("td").getBoundingClientRect();
        return {
          fits: tagRect.left >= cellRect.left && tagRect.right <= cellRect.right,
          text: tag.textContent,
          whiteSpace: getComputedStyle(tag).whiteSpace,
        };
      });
      expect(droppedTagFit).toEqual({
        fits: true,
        text: "withdrew",
        whiteSpace: "nowrap",
      });
    });
  }
}

test("tie records widen the W-L column to its widest value", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await renderStandings(page, { includeFirsts: true, includeTies: false });
  const wholeRecordWidth = await page.locator("th.standings-record").evaluate(
    (element) => element.getBoundingClientRect().width,
  );

  await renderStandings(page, { includeFirsts: true, includeTies: true });
  const tieRecordWidth = await page.locator("th.standings-record").evaluate(
    (element) => element.getBoundingClientRect().width,
  );

  expect(tieRecordWidth).toBeGreaterThan(wholeRecordWidth);
  await expectNumericCellsToFit(page);
});

test("desktop standings fill the container without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await renderStandings(page, { includeFirsts: true });

  const dimensions = await page.getByTestId("standings-scroll").evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
  await expectNumericCellsToFit(page);

  const measurements = await cellLineCounts(
    page.locator(".standings-table th, .standings-table td"),
  );
  expect(
    measurements
      .filter(({ lines }) => lines > 1)
      .every(({ classes }) => classes.includes("standings-player")),
  ).toBe(true);
});
