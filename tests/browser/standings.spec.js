const fs = require("node:fs");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const longPlayerName = "Christopher Alexander-Lewis";
const longPlayerLabel = `${longPlayerName} (#12)`;

const stylesheet = fs.readFileSync(
  path.join(__dirname, "../../static/css/style.css"),
  "utf8",
);

function standingsTable({ includeFirsts }) {
  const firstsHeading = includeFirsts ? '<th class="num">Firsts</th>' : "";
  const rows = [
    ["Alec Sjöholm", "1", "16-4", "1977", "10"],
    ["Jennifer Clinchy", "7", "14-6", "1032", "9"],
    [longPlayerName, "12", "10-10", "576", "10"],
  ]
    .map(([player, number, score, spread, firsts]) => `
      <tr>
        <td class="num">1</td>
        <td class="standings-player" title="${player} (#${number})">
          <span class="standings-player-line">
            <span class="standings-player-name">${player}</span>
            <span class="standings-player-number">(#${number})</span>
          </span>
        </td>
        <td class="num">${score}</td>
        <td class="num">${spread}</td>
        ${includeFirsts ? `<td class="num">${firsts}</td>` : ""}
      </tr>`)
    .join("");

  return `
    <div class="table-scroll table-narrow" data-testid="standings-scroll">
      <table class="standings-table">
        <thead>
          <tr>
            <th class="num">Rank</th>
            <th class="standings-player">Player</th>
            <th class="num">Score</th>
            <th class="num">Spread</th>
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
          <div class="page">${standingsTable(options)}</div>
        </main>
      </body>
    </html>`);
}

async function lineCounts(locator) {
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
        text: cell.textContent.trim(),
        lines: lineTops.size,
        whiteSpace: getComputedStyle(cell).whiteSpace,
      };
    }),
  );
}

for (const width of [320, 375]) {
  for (const includeFirsts of [false, true]) {
    test(`mobile standings fit at ${width}px${includeFirsts ? " with Firsts" : ""}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 812 });
      await renderStandings(page, { includeFirsts });

      const cells = page.locator(
        ".standings-table th, .standings-table td:not(.standings-player), .standings-player-name, .standings-player-number",
      );
      const measurements = await lineCounts(cells);
      expect(measurements).not.toHaveLength(0);
      for (const measurement of measurements) {
        expect(measurement, measurement.text).toMatchObject({
          lines: 1,
          whiteSpace: "nowrap",
        });
      }

      const playerLinesStayInline = await page
        .locator(".standings-player-line")
        .evaluateAll((lines) => lines.every((line) => {
          const tops = [...line.children].map((child) =>
            Math.round(child.getBoundingClientRect().top),
          );
          return new Set(tops).size === 1;
        }));
      expect(playerLinesStayInline).toBe(true);

      const overflow = await page.getByTestId("standings-scroll").evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
      }));
      expect(overflow.scrollWidth).toBe(overflow.clientWidth);
      expect(overflow.pageWidth).toBeLessThanOrEqual(overflow.viewportWidth);

      const longPlayer = page.locator(`td[title="${longPlayerLabel}"]`);
      const longName = longPlayer.locator(".standings-player-name");
      const truncation = await longName.evaluate((name) => ({
        clientWidth: name.clientWidth,
        scrollWidth: name.scrollWidth,
        textOverflow: getComputedStyle(name).textOverflow,
      }));
      expect(truncation.textOverflow).toBe("ellipsis");
      if (width === 320 || includeFirsts) {
        expect(truncation.scrollWidth).toBeGreaterThan(truncation.clientWidth);
      }

      const number = longPlayer.locator(".standings-player-number");
      expect(await number.innerText()).toBe("(#12)");
      expect(await longPlayer.getAttribute("title")).toBe(longPlayerLabel);
      const numberFit = await number.evaluate((element) => {
        const numberRect = element.getBoundingClientRect();
        const cellRect = element.closest("td").getBoundingClientRect();
        return numberRect.right <= cellRect.right;
      });
      expect(numberFit).toBe(true);
    });
  }
}

test("desktop standings fill the container without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await renderStandings(page, { includeFirsts: true });

  const dimensions = await page.getByTestId("standings-scroll").evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);

  const measurements = await lineCounts(
    page.locator(
      ".standings-table th, .standings-table td:not(.standings-player), .standings-player-name, .standings-player-number",
    ),
  );
  expect(measurements.every(({ lines }) => lines === 1)).toBe(true);
});
