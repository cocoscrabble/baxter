const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/browser",
  reporter: "line",
  use: {
    browserName: "chromium",
  },
});
